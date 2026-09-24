"""Surface invariant: what the server offers is exactly what it accepts.

For every turn state below, each (action, mode, weapon, target) the combat surface offers
must commit, and each declared combination it leaves out must be refused. Every option is
tried on a freshly rebuilt room so one accepted request cannot mask the next.
"""

import json

import pytest

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import DECLINE_REACTION, END_TURN_ACTION, REACTION_ACTION, _combat_data
from core.dice_engine import seed_dice
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from gateway.combat_actions import combat_surface, load_encounter, resolve_action
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

CHAT = "tui:group:surface-invariant"
KEEPER = AgentCtx(chat_key=CHAT, user_id="keeper-1", platform="tui", locale="en", extra={"role": "keeper"})


async def _room():
    """Solo play as in live testing: the keeper's own PC against two troop NPCs."""
    seed_dice(20260924)
    services = build_services(
        Settings(locale="en", default_rulepack="dh2"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )
    catalog = load_item_catalog(load_rulepack("dh2"))
    kardel = CharacterSheet("Kardel", "dh2")
    kardel.attributes.update({"BS": 45, "WS": 40, "S": 35, "T": 35, "Ag": 99, "WOUNDS": 12, "DAMAGE": 0})
    kardel.equipment = [
        ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 40}),  # below a full clip
        ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 30}),
        ItemInstance.create(catalog.resolve("sword")),
    ]
    await services.characters.save_character("keeper-1", CHAT, kardel)
    router = CommandRouter(services)
    for name in ("Cultist", "Heretic"):
        assert (await router.dispatch(KEEPER, f".npc create hive_scum | {name}")).startswith("✅")
    assert not (await router.dispatch(KEEPER, ".combat start Cultist, Heretic")).startswith("❌")
    for step in range(6):
        current = (await load_encounter(services, CHAT)).current_actor
        if current == "Kardel":
            break
        assert (await resolve_action(services, KEEPER, {"id": f"skip-{step}", "actor": current, "action": END_TURN_ACTION}))["ok"]
    return services, kardel


def _weapon(sheet, profile):
    return next(item.instance_id for item in sheet.equipment if item.profile_id == profile)


async def _do(services, request):
    outcome = await resolve_action(services, KEEPER, request)
    assert outcome["ok"], (request, outcome)
    return outcome


async def _attack_and_decline(services, request):
    outcome = await _do(services, request)
    pending = outcome["result"]["pending_reaction"]
    if pending is not None:
        await _do(services, {
            "id": request["id"] + "-decline", "actor": request["target"], "action": REACTION_ACTION,
            "mode": DECLINE_REACTION, "pending_id": pending["id"],
        })


def _shot(sheet, mode, profile, tag):
    return {
        "id": tag, "actor": "Kardel", "target": "Cultist", "action": "ranged_attack", "mode": mode,
        "weapon_instance_id": _weapon(sheet, profile),
    }


async def _prefix(services, sheet, name):
    if name == "turn_start":
        return
    if name == "after_aim_half":
        await _do(services, {"id": "aim", "actor": "Kardel", "action": "aim", "mode": "half", "weapon_instance_id": _weapon(sheet, "lasgun")})
    elif name == "after_standard_attack":
        await _attack_and_decline(services, _shot(sheet, "single", "lasgun", "std"))
    elif name == "after_semi_burst":
        await _attack_and_decline(services, _shot(sheet, "semi", "lasgun", "semi"))
    elif name == "after_full_burst":
        await _attack_and_decline(services, _shot(sheet, "full", "autogun", "full"))
    elif name == "after_melee":
        await _attack_and_decline(services, {
            "id": "cut", "actor": "Kardel", "target": "Cultist", "action": "melee_attack", "mode": "single",
            "weapon_instance_id": _weapon(sheet, "sword"),
        })
    elif name == "after_reload":
        await _do(services, {"id": "reload", "actor": "Kardel", "action": "reload", "mode": "full", "weapon_instance_id": _weapon(sheet, "lasgun")})
    elif name == "after_aim_then_attack":
        await _do(services, {"id": "aim", "actor": "Kardel", "action": "aim", "mode": "half", "weapon_instance_id": _weapon(sheet, "lasgun")})
        await _attack_and_decline(services, _shot(sheet, "single", "lasgun", "aimed"))
    elif name == "target_defeated":
        row = await services.documents.get(CHAT, "sheet", "Cultist")
        data = dict(row.data)
        data["attributes"] = {**data["attributes"], "DAMAGE": 9}  # at Wounds: next damage is Critical
        await services.documents.put(CHAT, "sheet", "Cultist", data)
        # A server roll may miss; retry across fresh turns until the troop is taken out.
        for index in range(20):
            current = (await load_encounter(services, CHAT)).current_actor
            if current != "Kardel":
                await _do(services, {"id": f"npc-pass-{index}", "actor": current, "action": END_TURN_ACTION})
                continue
            await _attack_and_decline(services, {**_shot(sheet, "single", "lasgun", f"kill-{index}"), "distance": 1})
            if (await load_encounter(services, CHAT)).combatants["Cultist"].defeated:
                break
            await _do(services, {"id": f"pass-{index}", "actor": "Kardel", "action": END_TURN_ACTION})
        assert (await load_encounter(services, CHAT)).combatants["Cultist"].defeated
        # Reach a fresh Kardel turn so the defeated target is the only difference.
        for index in range(6):
            current = (await load_encounter(services, CHAT)).current_actor
            await _do(services, {"id": f"lap-{index}", "actor": current, "action": END_TURN_ACTION})
            if (await load_encounter(services, CHAT)).current_actor == "Kardel":
                break
    elif name == "new_round":
        for index in range(3):  # Kardel, then both NPCs: back to Kardel in round two
            current = (await load_encounter(services, CHAT)).current_actor
            await _do(services, {"id": f"round-{index}", "actor": current, "action": END_TURN_ACTION})
        assert (await load_encounter(services, CHAT)).round_number == 2
    else:  # pragma: no cover
        raise AssertionError(name)


_STATES = [
    "turn_start", "after_aim_half", "after_standard_attack", "after_semi_burst", "after_full_burst",
    "after_melee", "after_reload", "after_aim_then_attack", "target_defeated", "new_round",
]


def _declared_candidates(sheet):
    """Every (action, mode, weapon, target) the pack declares, whether or not it fits now."""
    data = _combat_data(load_rulepack("dh2"))
    for action, definition in data["actions"].items():
        for mode in definition["modes"]:
            for item in sheet.equipment:
                targets = ["Cultist", "Heretic"] if action in {"ranged_attack", "melee_attack"} else [None]
                for target in targets:
                    yield action, mode, item.instance_id, target


def _offered(surface):
    offered = set()
    for action in surface["actions"]:
        for mode in action["modes"]:
            for weapon in mode["weapons"]:
                for target in action["targets"] or [None]:
                    offered.add((action["id"], mode["id"], weapon["id"], target))
    return offered


@pytest.mark.parametrize("state_name", _STATES)
async def test_every_offered_action_is_accepted_and_nothing_else_is(state_name):
    services, sheet = await _room()
    await _prefix(services, sheet, state_name)
    surface = await combat_surface(services, KEEPER, sheet)
    assert surface is not None and surface["actor"] == "Kardel"
    assert "end_turn" in surface
    offered = _offered(surface)
    if state_name == "target_defeated":
        assert all(target != "Cultist" for *_rest, target in offered)

    for index, (action, mode, weapon, target) in enumerate(sorted(_declared_candidates(sheet), key=str)):
        services, fresh = await _room()
        await _prefix(services, fresh, state_name)
        # Instance ids are minted per room: map by the weapon's profile.
        profile = next(item.profile_id for item in sheet.equipment if item.instance_id == weapon)
        request = {
            "id": f"probe-{index}", "actor": "Kardel", "action": action, "mode": mode,
            "weapon_instance_id": _weapon(fresh, profile),
        }
        if target is not None:
            request["target"] = target
        outcome = await resolve_action(services, KEEPER, request)
        assert outcome["ok"] is ((action, mode, weapon, target) in offered), (
            state_name, action, mode, profile, target, outcome.get("validation_failure"),
        )


@pytest.mark.parametrize("mode", ["single", "semi"])
async def test_every_offered_reaction_choice_is_accepted(mode):
    """The pending-reaction state: no turn actions, and each defender choice commits."""
    services, sheet = await _room()
    shot = await _do(services, {**_shot(sheet, mode, "lasgun", "shot"), "distance": 1})
    pending = shot["result"]["pending_reaction"]
    if pending is None:
        pytest.skip("the seeded roll missed; no reaction window to probe")
    surface = await combat_surface(services, KEEPER, sheet)
    assert surface["actions"] == [] and "end_turn" not in surface
    choices = [choice["id"] for choice in surface["reaction"]["choices"]]
    assert choices[-1] == DECLINE_REACTION
    for choice in choices:
        services, fresh = await _room()
        shot = await _do(services, {**_shot(fresh, mode, "lasgun", "shot"), "distance": 1})
        answered = await resolve_action(services, KEEPER, {
            "id": f"answer-{choice}", "actor": "Cultist", "action": REACTION_ACTION, "mode": choice,
            "pending_id": shot["result"]["pending_reaction"]["id"],
        })
        assert answered["ok"], (choice, answered)
    # A turn action while the reaction is pending is refused.
    services, fresh = await _room()
    await _do(services, {**_shot(fresh, mode, "lasgun", "shot"), "distance": 1})
    blocked = await resolve_action(services, KEEPER, {"id": "early", "actor": "Kardel", "action": END_TURN_ACTION})
    assert blocked["ok"] is False and json.dumps(blocked)
