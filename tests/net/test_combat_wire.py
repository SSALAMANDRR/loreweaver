"""Encounter MVP over the real wire: keeper + acting player + a third, uninvolved player."""

import asyncio
import json

import websockets

from agent import npc as npc_records
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import COMBAT_STATE_KEY, END_TURN_ACTION, REACTION_ACTION
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from gateway.combat_actions import load_encounter
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text
from net.keystore import Keystore, member_id_for_key
from net.tui_server import TuiServer
from tests.net.test_tui_server import _connect_and_join, _recv, _recv_until, _start

ROOM = "combat-wire"
SECRET = "SECRET-AGENDA-SENTINEL"


def _responder(seen, *, fail=False):
    def respond(messages, tools):
        seen.append((messages, tools))
        if fail:
            raise RuntimeError("narration provider down")
        return assistant_text("Steel rings against steel.")

    return respond


async def _room(*, fail_narration=False):
    seen: list = []
    services = build_services(
        Settings(locale="en"),
        llm=FakeLLM(responder=_responder(seen, fail=fail_narration)),
        embeddings=FakeEmbeddings(64),
    )
    chat_key = SessionSource(platform="tui", chat_type="group", chat_id=ROOM).chat_key()
    keystore = Keystore()
    keys = {
        "keeper": keystore.add(room=ROOM, name="GM", role="keeper"),
        "p1": keystore.add(room=ROOM, name="Player One", role="player"),
        "p2": keystore.add(room=ROOM, name="Player Two", role="player"),
    }
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    ada = CharacterSheet("Ada", "dh2")
    ada.attributes.update({"BS": 100, "WS": 100, "S": 40, "T": 30, "Ag": 99, "Dodge": 1, "Parry": 1, "DAMAGE": 0})
    ada.equipment = [
        ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 20}),
        ItemInstance.create(catalog.resolve("sword")),
    ]
    bea = CharacterSheet("Bea", "dh2")
    bea.attributes.update({"Ag": 1, "T": 30})
    await services.characters.save_character(member_id_for_key(keys["p1"]), chat_key, ada)
    await services.characters.save_character(member_id_for_key(keys["p2"]), chat_key, bea)
    record = await npc_records.create_npc(
        services.documents, chat_key, "Cultist", public_description="A hooded cultist.",
        secret_agenda=SECRET, stat_char="Cultist",
    )
    npc = CharacterSheet("Cultist", "dh2")
    npc.attributes.update({"BS": 100, "WS": 77, "S": 30, "T": 30, "Ag": 0, "Parry": 1, "DAMAGE": 0})
    npc.equipment = [ItemInstance.create(catalog.resolve("knife"))]
    await services.documents.put(chat_key, "sheet", "Cultist", dict(npc.to_dict(), owner=f"npc:{record.id}"))
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    return services, server, url, keys, chat_key, ada, seen


async def _state_where(ws, predicate):
    while True:
        frame = await _recv_until(ws, "state")
        if predicate(frame):
            return frame


async def _rejoin(url, key):
    """Join and return the socket plus the latest join-time state frame (extra
    broadcast states may interleave with the join sequence)."""
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"type": "join", "key": key}))
    state = None
    while True:
        frame = await _recv(ws)
        if frame["type"] == "state":
            state = frame
        elif frame["type"] == "ui_manifest" and state is not None:
            return ws, state


async def _result_for(ws, request_id):
    while True:
        frame = await _recv_until(ws, "action_result")
        if frame["id"] == request_id:
            return frame


def _has_encounter(frame):
    return bool(frame.get("combat", {}).get("state"))


async def _start_encounter(url, keys):
    sockets = {}
    for role in ("keeper", "p1", "p2"):
        ws, _welcome, _presence, _state = await _connect_and_join(url, keys[role])
        sockets[role] = ws
    await sockets["keeper"].send(json.dumps({"type": "input", "text": ".combat start Cultist"}))
    states = {role: await _state_where(ws, _has_encounter) for role, ws in sockets.items()}
    return sockets, states


async def _close(sockets, server):
    for ws in sockets.values():
        await ws.close()
    await server.close()


def _sword(ada):
    return next(item.instance_id for item in ada.equipment if item.profile_id == "sword")


async def test_encounter_reaction_flow_is_projected_per_viewer_and_narrated_after_commit():
    services, server, url, keys, chat_key, ada, seen = await _room()
    sockets, states = await _start_encounter(url, keys)
    try:
        assert states["p1"]["combat"]["actor"] == "Ada" and states["p1"]["combat"]["actions"]
        assert states["p2"]["combat"]["actions"] == [] and "end_turn" not in states["p2"]["combat"]
        assert states["keeper"]["combat"]["state"]["order"][0]["name"] == "Ada"
        for role in ("p1", "p2"):
            assert "Cultist" not in states[role]["combat"]["state"]["combatants"]
            assert SECRET not in json.dumps(states[role])

        await sockets["p1"].send(json.dumps({
            "type": "action_request", "id": "swing", "actor": "Ada", "target": "Cultist",
            "action": "melee_attack", "mode": "single", "weapon_instance_id": _sword(ada),
        }))
        results = {role: await _recv_until(ws, "action_result") for role, ws in sockets.items()}
        assert all(result["ok"] for result in results.values())
        assert "combat_state_before" in results["keeper"]["result"]["state_delta"]
        for role in ("p1", "p2"):
            assert "combat_state_before" not in results[role]["result"]["state_delta"]
            assert "choices" not in results[role]["result"]["pending_reaction"]
        keeper_state = await _state_where(sockets["keeper"], lambda f: "reaction" in f.get("combat", {}))
        offer = keeper_state["combat"]["reaction"]
        assert [choice["id"] for choice in offer["choices"]] == ["parry", "dodge", "decline"]
        bystander = await _state_where(sockets["p2"], lambda f: f["combat"]["state"]["pending_reaction"])
        assert "reaction" not in bystander["combat"]
        assert not seen  # no narration while the defender is still choosing

        await sockets["keeper"].send(json.dumps({
            "type": "action_request", "id": "parry", "actor": "Cultist", "action": REACTION_ACTION,
            "mode": "parry", "pending_id": offer["id"],
        }))
        final = {role: await _recv_until(ws, "action_result") for role, ws in sockets.items()}
        assert final["keeper"]["result"]["reaction"]["target"] == 77  # the NPC's ParryTarget: keeper-only
        assert "target" not in final["p2"]["result"]["reaction"]
        narrative = await _recv_until(sockets["p2"], "narrative")
        assert narrative["speaker"] == "kp"
        assert len(seen) == 1 and seen[0][1] is None
        prompt = seen[0][0][-1]["content"]
        assert SECRET not in prompt and "\"target\": 77" not in prompt
        assert "A hooded cultist." in prompt
        assert json.dumps(final["p2"]["result"], ensure_ascii=False) in prompt
    finally:
        await _close(sockets, server)


async def test_reconnect_restores_the_encounter_and_the_pending_reaction_offer():
    services, server, url, keys, chat_key, ada, seen = await _room()
    sockets, _states = await _start_encounter(url, keys)
    try:
        await sockets["p1"].send(json.dumps({
            "type": "action_request", "id": "swing", "actor": "Ada", "target": "Cultist",
            "action": "melee_attack", "mode": "single", "weapon_instance_id": _sword(ada),
        }))
        await _state_where(sockets["keeper"], lambda f: "reaction" in f.get("combat", {}))
        await _state_where(sockets["p1"], lambda f: f["combat"]["state"]["pending_reaction"])
        for role in ("keeper", "p1"):
            await sockets.pop(role).close()
        keeper_ws, keeper_state = await _rejoin(url, keys["keeper"])
        sockets["keeper"] = keeper_ws
        player_ws, player_state = await _rejoin(url, keys["p1"])
        sockets["p1"] = player_ws
        assert keeper_state["combat"]["reaction"]["actor"] == "Cultist"
        view = player_state["combat"]["state"]
        assert view["round_number"] == 1 and view["current_actor"] == "Ada"
        assert view["pending_reaction"]["defender"] == "Cultist"
        assert player_state["combat"]["actions"] == [] and "reaction" not in player_state["combat"]
    finally:
        await _close(sockets, server)


async def test_narration_failure_after_commit_keeps_the_committed_result():
    services, server, url, keys, chat_key, ada, seen = await _room(fail_narration=True)
    sockets, _states = await _start_encounter(url, keys)
    try:
        await sockets["p1"].send(json.dumps({
            "type": "action_request", "id": "swing", "actor": "Ada", "target": "Cultist",
            "action": "melee_attack", "mode": "single", "weapon_instance_id": _sword(ada),
        }))
        pending = await _recv_until(sockets["keeper"], "action_result")
        await sockets["keeper"].send(json.dumps({
            "type": "action_request", "id": "decline", "actor": "Cultist", "action": REACTION_ACTION,
            "mode": "decline", "pending_id": pending["result"]["pending_reaction"]["id"],
        }))
        final = await _result_for(sockets["p1"], "decline")
        assert final["ok"]
        damage = final["result"]["final_damage"]
        saved = json.loads((await services.store.doc_get(chat_key, "sheet", "Cultist"))["data"])
        assert saved["attributes"]["DAMAGE"] == damage
        state = await load_encounter(services, chat_key)
        assert state.pending_reaction is None and state.current_actor == "Ada"
        # The room keeps working: the same player can still end the turn.
        await sockets["p1"].send(json.dumps({"type": "action_request", "id": "end", "actor": "Ada", "action": END_TURN_ACTION}))
        ended = await _result_for(sockets["p1"], "end")
        assert ended["ok"] and (await load_encounter(services, chat_key)).current_actor != "Ada"
        # The room lock ran the (failing) narration before this request: it was attempted once.
        assert len(seen) == 1
    finally:
        await _close(sockets, server)


async def test_simultaneous_requests_in_one_room_apply_exactly_once():
    services, server, url, keys, chat_key, ada, seen = await _room()
    sockets, _states = await _start_encounter(url, keys)
    try:
        before = await load_encounter(services, chat_key)
        # A second connection for the same player races the first one.
        twin, *_ = await _connect_and_join(url, keys["p1"])
        sockets["twin"] = twin
        frames = [
            {"type": "action_request", "id": "end-a", "actor": "Ada", "action": END_TURN_ACTION},
            {"type": "action_request", "id": "end-b", "actor": "Ada", "action": END_TURN_ACTION},
        ]
        await asyncio.gather(sockets["p1"].send(json.dumps(frames[0])), twin.send(json.dumps(frames[1])))

        outcomes = await asyncio.gather(_result_for(sockets["p1"], "end-a"), _result_for(twin, "end-b"))
        assert sorted(outcome["ok"] for outcome in outcomes) == [False, True]
        after = await load_encounter(services, chat_key)
        assert after.order.index(after.current_actor) == before.order.index(before.current_actor) + 1
        # The same request id replayed is refused, not re-applied.
        await twin.send(json.dumps(frames[1]))
        replay = await _result_for(twin, "end-b")
        assert replay["ok"] is False
        assert (await load_encounter(services, chat_key)).current_actor == after.current_actor
        raw = await services.store.state_get(chat_key, COMBAT_STATE_KEY)
        assert json.loads(raw)["recent_requests"].count("end-a") + json.loads(raw)["recent_requests"].count("end-b") == 1
    finally:
        await _close(sockets, server)


async def test_players_cannot_start_an_encounter():
    services, server, url, keys, chat_key, ada, seen = await _room()
    ws, *_ = await _connect_and_join(url, keys["p1"])
    try:
        await ws.send(json.dumps({"type": "input", "text": ".combat start Cultist"}))
        await _recv(ws)
        assert await services.store.state_get(chat_key, COMBAT_STATE_KEY) is None
    finally:
        await ws.close()
        await server.close()


async def test_protocol_version_advertised_is_2_7():
    services, server, url, keys, chat_key, ada, seen = await _room()
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "join", "key": keys["p1"]}))
            welcome = await _recv(ws)
            assert welcome["protocol"] == "2.7"
    finally:
        await server.close()
