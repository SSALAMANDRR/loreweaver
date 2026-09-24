"""The whole solo-play combat loop over the wire, as a keeper plays it in Studio.

A keeper creates their own DH2 PC through the real staged creation flow, materializes
a canonical Troop, opens an encounter, and fights it to the end with manual and server
rolls, reactions on both sides and NPC turns, never touching the store by hand. The fight
must end by itself on the deciding commit (no one-PC turn loop), the panel must leave the
state frame, and the next message must be an ordinary Keeper turn. While the fight is
open, a free-text "finish him" must not become a Keeper skill check that kills outside
the engine (the live-play bug); afterwards, finishing off the fallen troop is narration.
"""

import json
from types import SimpleNamespace

from agent.context import AgentCtx
from agent.services import build_services
from core.combat import COMBAT_AFTERMATH_KEY, COMBAT_STATE_KEY, DECLINE_REACTION, END_TURN_ACTION, REACTION_ACTION
from core.dice_engine import seed_dice
from gateway.commands import CommandRouter
from gateway.ops import RateLimiter
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.llm import FakeLLM, assistant_text, assistant_tools, tool_call
from net.keystore import Keystore, member_id_for_key
from net.tui_server import TuiServer
from tests.net.test_tui_server import _connect_and_join, _recv, _start

ROOM = "combat-full-scenario"
CREATION = [
    ".dh2 Мир-улей | Кардел",
    ".create done",
    ".create Астра Милитарум | trained_skill=Медика | weapon_package=Лазган | aptitude=Полевое",
    ".create Хирургеон | role_talent=Нокдаун",
    ".create Aptitudes=Навык Стрельбы",
    ".create done",
]
ACQUISITIONS = [".create Нож", ".create Цепной клинок", ".create Стаб-револьвер", ".create Медпакет", ".create Лук"]
FINISH_HIM = "Кардел делает контрольный в голову культисту"
SEARCH = "Кардел обыскивает тело культиста"


class Keeper:
    """The model side: records every call, and on the finish-him line tries the exact
    mechanical shortcut the live Keeper took (a WS skill check)."""

    def __init__(self):
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append((messages, tools))
        last = messages[-1]
        if tools and last.get("role") == "user" and FINISH_HIM in str(last.get("content")):
            return assistant_tools(tool_call("skill_check", skill_name="WS"))
        return assistant_text("Сцена продолжается.")


async def _frames_until(ws, predicate, limit=400):
    seen = []
    for _ in range(limit):
        frame = await _recv(ws)
        seen.append(frame)
        if predicate(frame):
            return frame, seen
    raise AssertionError("expected frame never arrived")


async def _state(ws, predicate=lambda frame: True):
    frame, _seen = await _frames_until(ws, lambda f: f.get("type") == "state" and predicate(f))
    return frame


async def _result(ws, request_id):
    frame, _seen = await _frames_until(ws, lambda f: f.get("type") == "action_result" and f.get("id") == request_id)
    return frame


async def _act(ws, request):
    await ws.send(json.dumps({"type": "action_request", **request}))
    outcome = await _result(ws, request["id"])
    assert outcome["ok"], (request, outcome)
    return outcome


async def test_a_keeper_plays_a_whole_fight_from_creation_to_the_next_scene():
    seed_dice(424242)
    model = Keeper()
    services = build_services(
        Settings(locale="en", default_rulepack="dh2"), llm=FakeLLM(responder=model), embeddings=FakeEmbeddings(64)
    )
    chat_key = SessionSource(platform="tui", chat_type="group", chat_id=ROOM).chat_key()
    keystore = Keystore()
    key = keystore.add(room=ROOM, name="GM", role="keeper")
    keeper = AgentCtx(chat_key=chat_key, user_id=member_id_for_key(key), platform="tui", locale="en", extra={"role": "keeper"})
    router = CommandRouter(services)

    # 1-2. The keeper's own PC through the real creation flow (the commands Studio sends).
    for command in CREATION:
        reply = await router.dispatch(keeper, command)
        assert reply is not None and "cannot be applied" not in reply, (command, reply)
    for command in ACQUISITIONS:
        if "choices are complete" in await router.dispatch(keeper, command):
            break
    real_roll = services.dice.roll_expression
    services.dice.roll_expression = lambda expression: SimpleNamespace(total=100)
    assert "ready for play" in await router.dispatch(keeper, ".finalize roll")
    services.dice.roll_expression = real_roll
    # 3. A canonical Troop from the pack's opponent profiles.
    assert (await router.dispatch(keeper, ".npc create hive_scum | Культист")).startswith("✅")

    server = TuiServer(services, keystore, port=0)
    # A whole fight is dozens of requests in a few seconds; this test is about combat,
    # not about the per-member throttle.
    server.rate_limiter = RateLimiter(capacity=1000, refill_per_sec=1000.0)
    url = await _start(server)
    ws, _welcome, _presence, _joined = await _connect_and_join(url, key)
    try:
        # 4. The keeper opens the encounter; the panel appears.
        await ws.send(json.dumps({"type": "input", "text": ".combat start Культист"}))
        state = await _state(ws, lambda f: bool(f.get("combat", {}).get("state")))
        order = {entry["name"]: entry for entry in state["combat"]["state"]["order"]}
        # The keeper controls both, but only Кардел is their own character.
        assert order["Кардел"]["controlled"] and not order["Кардел"]["keeper_controlled"]
        assert order["Культист"]["controlled"] and order["Культист"]["keeper_controlled"]

        # 5. Physical dice for the keeper's own rolls.
        await ws.send(json.dumps({"type": "input", "text": ".rollmode manual"}))
        state = await _state(ws, lambda f: f.get("roll_mode") == "manual")

        async def current():
            raw = await services.store.state_get(chat_key, COMBAT_STATE_KEY)
            return json.loads(raw)["current_actor"] if raw else None

        async def surface():
            await ws.send(json.dumps({"type": "input", "text": ".combat status"}))
            return (await _state(ws, lambda f: "combat" in f))["combat"]

        def weapon(combat, action, mode, profile_label=None):
            entry = next(a for a in combat["actions"] if a["id"] == action)
            chosen = next(m for m in entry["modes"] if m["id"] == mode)
            return chosen["weapons"][0]["id"]

        # 6. Whoever rolled higher acts first; the NPC passes if it is first.
        if await current() == "Культист":
            await _act(ws, {"id": "npc-open", "actor": "Культист", "action": END_TURN_ACTION})

        # 7. Aim (half), then 8. a Standard Attack with a physical d100 at point blank.
        combat = await surface()
        assert combat["actor"] == "Кардел" and combat["end_turn"]
        lasgun = weapon(combat, "ranged_attack", "single")
        await _act(ws, {"id": "aim-1", "actor": "Кардел", "action": "aim", "mode": "half", "weapon_instance_id": lasgun})
        combat = await surface()
        assert {a["id"] for a in combat["actions"]} >= {"ranged_attack"}  # Aim leaves the attack half action
        shot = await _act(ws, {
            "id": "shot-1", "actor": "Кардел", "target": "Культист", "action": "ranged_attack", "mode": "single",
            "weapon_instance_id": lasgun, "distance": 2, "roll_source": "manual", "manual_rolls": {"attack": [50]},
        })
        assert shot["result"]["attack_roll"] == 50 and shot["result"]["roll_sources"]["attack"] == "manual"
        # 9. The NPC's controller (the keeper) answers the reaction window: Dodge on server dice.
        pending = shot["result"]["pending_reaction"]
        assert pending is not None, shot
        dodged = await _act(ws, {
            "id": "npc-dodge", "actor": "Культист", "action": REACTION_ACTION, "mode": "dodge", "pending_id": pending["id"],
        })
        assert dodged["result"]["reaction"]["type"] == "dodge"
        assert dodged["encounter_ended"] is False and not dodged["result"]["target_defeated"]
        # 10. End of Кардел's turn.
        assert await current() == "Кардел"
        await _act(ws, {"id": "end-1", "actor": "Кардел", "action": END_TURN_ACTION})

        # 11. While the fight is open, a free-text "finish him" must not become a check
        # that kills outside the engine.
        before_npc = json.loads((await services.store.doc_get(chat_key, "sheet", "Культист"))["data"])
        await ws.send(json.dumps({"type": "input", "text": FINISH_HIM}))
        _frame, seen = await _frames_until(ws, lambda f: f.get("type") == "narrative" and f.get("speaker") == "kp")
        assert not any(f.get("type") == "dice" for f in seen)  # no WS check was rolled
        tool_results = [m for m, _t in model.calls for m in m if m.get("role") == "tool"]
        assert tool_results and "combat controls" in str(tool_results[-1]["content"])
        after_npc = json.loads((await services.store.doc_get(chat_key, "sheet", "Культист"))["data"])
        assert after_npc == before_npc and await services.store.state_get(chat_key, COMBAT_STATE_KEY)

        # 12-13. The NPC's turn: it shoots or stabs Кардел; Кардел declines; the NPC ends its turn.
        assert await current() == "Культист"
        npc_state = await surface()
        npc_actions = {a["id"]: a for a in npc_state["actions"]}
        attack = "ranged_attack" if "ranged_attack" in npc_actions else "melee_attack"
        npc_weapon = npc_actions[attack]["modes"][0]["weapons"][0]["id"]
        npc_mode = npc_actions[attack]["modes"][0]["id"]
        swing = await _act(ws, {
            "id": "npc-attack", "actor": "Культист", "target": "Кардел", "action": attack, "mode": npc_mode,
            "weapon_instance_id": npc_weapon, **({"distance": 2} if attack == "ranged_attack" else {}),
        })
        if swing["result"]["pending_reaction"] is not None:
            await _act(ws, {
                "id": "pc-decline", "actor": "Кардел", "action": REACTION_ACTION, "mode": DECLINE_REACTION,
                "pending_id": swing["result"]["pending_reaction"]["id"],
            })
        await _act(ws, {"id": "npc-end-1", "actor": "Культист", "action": END_TURN_ACTION})

        # 14. Several rounds: Кардел fires on manual dice until the engine takes the troop out.
        final = None
        for index in range(15):
            if await current() == "Культист":
                await _act(ws, {"id": f"npc-pass-{index}", "actor": "Культист", "action": END_TURN_ACTION})
            combat = await surface()
            shot = await _act(ws, {
                "id": f"fire-{index}", "actor": "Кардел", "target": "Культист", "action": "ranged_attack",
                "mode": "single", "weapon_instance_id": weapon(combat, "ranged_attack", "single"), "distance": 2,
                "roll_source": "manual", "manual_rolls": {"attack": [11]},
            })
            final = shot
            if shot["result"]["pending_reaction"] is not None:
                final = await _act(ws, {
                    "id": f"npc-takes-{index}", "actor": "Культист", "action": REACTION_ACTION,
                    "mode": DECLINE_REACTION, "pending_id": shot["result"]["pending_reaction"]["id"],
                })
            if final["result"]["target_defeated"]:
                break
            await _act(ws, {"id": f"end-{index}", "actor": "Кардел", "action": END_TURN_ACTION})
        assert final is not None and final["result"]["target_defeated"] is True

        # 15. The deciding commit ended the encounter: no combat row, no panel, a notice.
        assert final["encounter_ended"] is True
        assert await services.store.state_get(chat_key, COMBAT_STATE_KEY) is None
        closed = await _state(ws, lambda f: "combat" not in f)
        assert closed["roll_mode"] == "manual"  # the dice preference survives the fight
        notice, _seen = await _frames_until(ws, lambda f: f.get("type") == "system")
        assert notice["text"] == get_i18n("en").t("combat.ended_notice")
        narration, _seen = await _frames_until(ws, lambda f: f.get("type") == "narrative" and f.get("speaker") == "kp")
        narration_call = next(m for m, t in reversed(model.calls) if t is None and "encounter_ended" in str(m[-1]["content"]))
        assert json.loads(narration_call[-1]["content"])["context"]["encounter_ended"] is True

        # 16. No one-PC loop: a late turn pass is refused, nothing is recreated.
        await ws.send(json.dumps({"type": "action_request", "id": "late", "actor": "Кардел", "action": END_TURN_ACTION}))
        assert (await _result(ws, "late"))["ok"] is False
        assert await services.store.state_get(chat_key, COMBAT_STATE_KEY) is None

        # 17. The next message is an ordinary Keeper turn that knows how the fight ended.
        calls_before = len(model.calls)
        await ws.send(json.dumps({"type": "input", "text": SEARCH}))
        _frame, seen = await _frames_until(ws, lambda f: f.get("type") == "narrative" and f.get("speaker") == "kp")
        assert not any(f.get("type") == "action_result" for f in seen)
        keeper_call = next(m for m, t in model.calls[calls_before:] if t)
        prompt = "\n".join(str(message.get("content")) for message in keeper_call)
        i18n = get_i18n("en")
        assert i18n.t("prompt.game_state.aftermath_contract") in prompt
        assert i18n.t("prompt.game_state.encounter_contract") not in prompt

        # 18. The fallen troop stays out: it cannot be pulled into a new encounter.
        aftermath = json.loads(await services.store.state_get(chat_key, COMBAT_AFTERMATH_KEY))
        assert {e["name"]: e["defeated"] for e in aftermath["combatants"]} == {"Кардел": False, "Культист": True}
        assert await router.dispatch(keeper, ".combat start Культист") == i18n.t("combat.command.start.defeated")
        assert await services.store.state_get(chat_key, COMBAT_STATE_KEY) is None
    finally:
        await ws.close()
        await server.close()
