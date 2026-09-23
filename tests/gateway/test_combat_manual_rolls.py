"""Physical dice in combat: the server declares the dice, the player enters faces,
the server validates and does all the math. Shares the `.rollmode` preference and the
`core.manual_roll` validator with ordinary checks."""

import pytest

from core.combat import DECLINE_REACTION, REACTION_ACTION
from core.documents import PLAYER_VIEWER
from gateway.combat_actions import combat_surface, project_action_frame, resolve_action
from gateway.commands import CommandRouter
from infra.i18n import get_i18n
from net.state import build_room_state
from tests.gateway.test_combat_action import CHAT, KEEPER, PLAYER, _scene, _snapshot, _weapon

ATTACK_SPEC = {"id": "attack", "label": "Attack roll", "expression": "1d100", "count": 1, "sides": 100}


def _shot(pc, **extra):
    return {
        "id": extra.pop("id", "shot"), "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"), **extra,
    }


async def test_the_surface_declares_the_dice_each_step_takes_and_the_players_mode():
    services, pc, _npc = await _scene()
    surface = await combat_surface(services, PLAYER, pc)
    assert (await build_room_state(services, PLAYER))["roll_mode"] == "auto"
    modes = {(action["id"], mode["id"]): mode for action in surface["actions"] for mode in action["modes"]}
    assert modes[("ranged_attack", "single")]["manual_rolls"] == [ATTACK_SPEC]
    assert modes[("melee_attack", "single")]["manual_rolls"] == [ATTACK_SPEC]
    assert modes[("aim", "half")]["manual_rolls"] == []
    router = CommandRouter(services)
    await router.dispatch(PLAYER, ".rollmode manual")
    assert (await build_room_state(services, PLAYER))["roll_mode"] == "manual"


async def test_a_manual_attack_uses_exactly_the_entered_face_and_is_labelled_manual():
    services, pc, _npc = await _scene()
    pending = await resolve_action(services, PLAYER, _shot(pc, roll_source="manual", manual_rolls={"attack": [57]}))
    assert pending["ok"], pending
    assert pending["result"]["attack_roll"] == 57
    assert pending["result"]["roll_sources"] == {"attack": "manual"}
    offer = (await combat_surface(services, KEEPER))["reaction"]
    # Ranged: Dodge only, plus decline (which takes no dice).
    assert [choice["manual_rolls"] for choice in offer["choices"]] == [
        [{**ATTACK_SPEC, "id": "reaction", "label": "Reaction roll"}],
        [],
    ]
    final = await resolve_action(services, KEEPER, {
        "id": "decline", "actor": "Cultist", "action": REACTION_ACTION, "mode": DECLINE_REACTION,
        "pending_id": pending["result"]["pending_reaction"]["id"], "roll_source": "manual", "manual_rolls": {},
    })
    assert final["ok"], final
    assert final["result"]["attack_roll"] == 57
    # Damage is still rolled by the server, and says so.
    assert final["result"]["roll_sources"] == {"attack": "manual", "damage": "server"}
    public = await project_action_frame(services, CHAT, final, PLAYER_VIEWER)
    assert public["result"]["roll_sources"] == {"attack": "manual", "damage": "server"}


async def test_the_automatic_path_is_unchanged_and_labelled_server():
    services, pc, _npc = await _scene()
    pending = await resolve_action(services, PLAYER, _shot(pc))
    assert pending["ok"] and pending["result"]["roll_sources"] == {"attack": "server"}
    assert 1 <= pending["result"]["attack_roll"] <= 100


async def test_a_manual_reaction_uses_the_entered_face():
    services, pc, _npc = await _scene()
    pending = await resolve_action(services, PLAYER, {
        "id": "swing", "actor": "Ada", "target": "Cultist", "action": "melee_attack", "mode": "single",
        "weapon_instance_id": _weapon(pc, "sword"),
    })
    final = await resolve_action(services, KEEPER, {
        "id": "parry", "actor": "Cultist", "action": REACTION_ACTION, "mode": "parry",
        "pending_id": pending["result"]["pending_reaction"]["id"],
        "roll_source": "manual", "manual_rolls": {"reaction": [99]},
    })
    assert final["ok"], final
    assert final["result"]["reaction"]["roll"] == 99 and final["result"]["reaction"]["success"] is True
    assert final["result"]["roll_sources"] == {"attack": "server", "reaction": "manual"}


@pytest.mark.parametrize(
    "extra",
    [
        {"roll_source": "manual", "manual_rolls": {"attack": [0]}},
        {"roll_source": "manual", "manual_rolls": {"attack": [101]}},
        {"roll_source": "manual", "manual_rolls": {"attack": [50, 7]}},
        {"roll_source": "manual", "manual_rolls": {"attack": ["57"]}},
        {"roll_source": "manual", "manual_rolls": {"attack": [True]}},
        {"roll_source": "manual", "manual_rolls": {"damage": [5]}},
        {"roll_source": "manual", "manual_rolls": {"attack": [57], "damage": [5]}},
        {"roll_source": "manual"},
        {"roll_source": "server", "manual_rolls": {"attack": [57]}},
        {"roll_source": "cheat", "manual_rolls": {"attack": [57]}},
    ],
)
async def test_malformed_or_out_of_range_dice_are_refused_without_mutation(extra):
    services, pc, _npc = await _scene()
    before = await _snapshot(services)
    refused = await resolve_action(services, PLAYER, _shot(pc, **extra))
    assert refused["ok"] is False
    assert refused["validation_failure"] == get_i18n("en").t("combat.invalid.manual_roll")
    assert await _snapshot(services) == before


async def test_utility_actions_take_no_dice_in_manual_mode():
    services, pc, _npc = await _scene()
    aim = {"id": "aim", "actor": "Ada", "action": "aim", "mode": "half", "weapon_instance_id": _weapon(pc, "lasgun")}
    assert (await resolve_action(services, PLAYER, {**aim, "roll_source": "manual", "manual_rolls": {}}))["ok"]
    refused = await resolve_action(services, PLAYER, {**aim, "id": "aim2", "roll_source": "manual", "manual_rolls": {"attack": [5]}})
    assert refused["ok"] is False


async def test_the_keeper_prompt_knows_a_player_rolls_physical_dice():
    from core.prompt_sections import inject_game_state_prompt

    services, _pc, _npc = await _scene()
    await CommandRouter(services).dispatch(PLAYER, ".rollmode manual")
    text = await inject_game_state_prompt(PLAYER, services.characters, services.store, get_i18n("en"))
    assert "Dice input for Ada: manual" in text


def _studio_frame(surface, action, mode, request_id, distance=None):
    """Build a request exactly as the Studio form does from a server surface."""
    frame = {
        "id": request_id, "actor": surface["actor"], "action": action["id"], "mode": mode["id"],
        "weapon_instance_id": mode["weapons"][0]["id"],
        "roll_source": "manual", "manual_rolls": {spec["id"]: [50] * spec["count"] for spec in mode["manual_rolls"]},
    }
    if action["targets"]:
        frame["target"] = action["targets"][0]
    if distance is not None and mode.get("accepts_distance"):
        frame["distance"] = distance
    return frame


async def test_after_manual_burst_reaction_and_end_turn_every_offered_action_is_accepted(caplog):
    from core.combat import END_TURN_ACTION
    from gateway.combat_actions import load_encounter

    services, pc, _npc = await _scene()
    burst = await resolve_action(services, PLAYER, _shot(pc, id="burst", mode="semi", roll_source="manual", manual_rolls={"attack": [1]}))
    assert burst["ok"], burst
    dodge = await resolve_action(services, KEEPER, {
        "id": "dodge", "actor": "Cultist", "action": REACTION_ACTION, "mode": "dodge",
        "pending_id": burst["result"]["pending_reaction"]["id"], "roll_source": "manual", "manual_rolls": {"reaction": [80]},
    })
    assert dodge["ok"] and dodge["result"]["hits"], dodge
    controllers = {"Ada": PLAYER, "Bea": _bystander(), "Cultist": KEEPER}
    for step in range(3):
        current = (await load_encounter(services, CHAT)).current_actor
        if current == "Cultist":
            break
        ended = await resolve_action(services, controllers[current], {"id": f"end-{step}", "actor": current, "action": END_TURN_ACTION})
        assert ended["ok"], ended

    # The invariant: whatever the fresh surface offers, sent as offered, is accepted —
    # including when the player typed a distance, which only ranged modes accept.
    for step in range(4):
        surface = await combat_surface(services, KEEPER)
        if not surface["actions"]:
            break
        assert surface["actor"] == "Cultist"
        action = surface["actions"][0]
        mode = action["modes"][0]
        assert mode["accepts_distance"] is (action["id"] == "ranged_attack")
        frame = _studio_frame(surface, action, mode, f"offered-{step}", distance=3)
        accepted = await resolve_action(services, KEEPER, frame)
        assert accepted["ok"], (frame, accepted)
        pending = accepted["result"].get("pending_reaction")
        if pending:
            answered = await resolve_action(services, PLAYER, {
                "id": f"take-{step}", "actor": "Ada", "action": REACTION_ACTION, "mode": DECLINE_REACTION,
                "pending_id": pending["id"],
            })
            assert answered["ok"], answered
    else:
        raise AssertionError("the surface never ran out of actions")


def _bystander():
    from tests.gateway.test_combat_action import BYSTANDER

    return BYSTANDER


async def test_a_melee_request_with_distance_is_refused_specifically_and_traced(caplog):
    import logging

    services, pc, _npc = await _scene()
    before = await _snapshot(services)
    with caplog.at_level(logging.INFO, logger="gateway.combat_actions"):
        refused = await resolve_action(services, PLAYER, {
            "id": "swing-far", "actor": "Ada", "target": "Cultist", "action": "melee_attack", "mode": "single",
            "weapon_instance_id": _weapon(pc, "sword"), "distance": 3,
        })
    assert refused["validation_failure"] == get_i18n("en").t("combat.invalid.distance")
    assert await _snapshot(services) == before
    trace = next(record.getMessage() for record in caplog.records if "combat action rejected" in record.getMessage())
    assert "distance is unavailable for this action" in trace
    assert '"current_actor": "Ada"' in trace and '"distance": 3' in trace
    assert '"accepts_distance": false' in trace and '"actor_turn_actions": []' in trace
