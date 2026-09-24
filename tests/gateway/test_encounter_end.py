"""An encounter ends by itself once one side has nobody left in the fight.

The engine deletes the encounter row in the same commit as the deciding action, keeps a
keeper-grade aftermath for the Keeper prompt, refuses to bring a defeated NPC back, and
keeps attack checks out of the Keeper's free-standing check tool while a fight is open.
"""

import json

from agent.context import AgentCtx
from agent.kp_tools_mechanics import DiceTools
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import (
    COMBAT_AFTERMATH_KEY,
    COMBAT_STATE_KEY,
    DECLINE_REACTION,
    END_TURN_ACTION,
    OPPOSITION_SIDE,
    PARTY_SIDE,
    REACTION_ACTION,
    CombatantState,
    CombatState,
    encounter_concluded,
)
from core.documents import PLAYER_VIEWER
from core.item_model import ItemInstance, load_item_catalog
from core.prompt_sections import inject_game_state_prompt
from core.rulepacks import load_rulepack
from gateway.combat_actions import (
    combat_surface,
    load_encounter,
    project_action_frame,
    resolve_action,
    should_narrate,
    start_room_encounter,
)
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.llm import FakeLLM

CHAT = "tui:group:encounter-end"


async def _solo_room(locale="ru"):
    """Solo play: the keeper runs the NPCs and plays their own PC Кардел."""
    services = build_services(
        Settings(locale=locale, default_rulepack="dh2"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )
    keeper = AgentCtx(chat_key=CHAT, user_id="keeper-1", platform="tui", locale=locale, extra={"role": "keeper"})
    catalog = load_item_catalog(load_rulepack("dh2"))
    kardel = CharacterSheet("Кардел", "dh2")
    kardel.attributes.update({"BS": 100, "WS": 45, "S": 35, "T": 35, "Ag": 99, "WOUNDS": 12, "DAMAGE": 0})
    kardel.equipment = [ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 60})]
    await services.characters.save_character("keeper-1", CHAT, kardel)
    return services, CommandRouter(services), keeper, kardel


async def _sheet(services, name):
    return json.loads((await services.store.doc_get(CHAT, "sheet", name))["data"])


async def _to_turn(services, keeper, actor, tag):
    for step in range(8):
        state = await load_encounter(services, CHAT)
        if state.current_actor == actor:
            return
        ended = await resolve_action(services, keeper, {"id": f"{tag}-{step}", "actor": state.current_actor, "action": END_TURN_ACTION})
        assert ended["ok"], ended
    raise AssertionError(f"{actor} never got a turn")


async def _shoot_until_defeated(services, keeper, kardel, target):
    """Кардел shoots `target` each turn (the NPC side passes) until the engine defeats it."""
    lasgun = kardel.equipment[0].instance_id
    for index in range(12):
        await _to_turn(services, keeper, "Кардел", f"{target}-{index}")
        shot = await resolve_action(services, keeper, {
            "id": f"shot-{target}-{index}", "actor": "Кардел", "target": target, "action": "ranged_attack",
            "mode": "single", "weapon_instance_id": lasgun,
        })
        assert shot["ok"], shot
        final = shot
        if shot["result"]["pending_reaction"] is not None:
            final = await resolve_action(services, keeper, {
                "id": f"decline-{target}-{index}", "actor": target, "action": REACTION_ACTION,
                "mode": DECLINE_REACTION, "pending_id": shot["result"]["pending_reaction"]["id"],
            })
            assert final["ok"], final
        if final["result"]["target_defeated"]:
            return final
        passed = await resolve_action(services, keeper, {"id": f"pass-{target}-{index}", "actor": "Кардел", "action": END_TURN_ACTION})
        assert passed["ok"], passed
        assert passed["encounter_ended"] is False
    raise AssertionError(f"{target} was never defeated")


async def test_defeating_the_last_opponent_ends_the_encounter_in_the_same_commit():
    services, router, keeper, kardel = await _solo_room()
    await router.dispatch(keeper, ".npc create hive_scum | Культист")
    assert not (await router.dispatch(keeper, ".combat start Культист")).startswith("❌")

    final = await _shoot_until_defeated(services, keeper, kardel, "Культист")

    assert final["encounter_ended"] is True
    assert await services.store.state_get(CHAT, COMBAT_STATE_KEY) is None
    assert await load_encounter(services, CHAT) is None
    # No surface: the client's combat panel disappears with the encounter.
    assert await combat_surface(services, keeper, kardel) is None
    aftermath = json.loads(await services.store.state_get(CHAT, COMBAT_AFTERMATH_KEY))
    outcome = {entry["name"]: entry for entry in aftermath["combatants"]}
    assert aftermath["reason"] == "concluded"
    assert outcome["Культист"]["defeated"] is True and outcome["Культист"]["side"] == OPPOSITION_SIDE
    assert outcome["Кардел"]["defeated"] is False and outcome["Кардел"]["side"] == PARTY_SIDE
    # The deciding frame is projected against the state it closed, not masked fail-closed.
    public = await project_action_frame(services, CHAT, final, PLAYER_VIEWER)
    assert public["result"]["actor"] == "Кардел" and public["result"]["target_defeated"] is True
    assert public["result"]["attack_target"] is not None  # the PC's own attack numbers stay public
    assert public["result"]["hits"][0]["tb_reduction"] is None  # the NPC's toughness stays masked
    assert should_narrate(final)
    # Further combat input is refused: the fight is over.
    after = await resolve_action(services, keeper, {"id": "late", "actor": "Кардел", "action": END_TURN_ACTION})
    assert after["ok"] is False


async def test_a_defeated_npc_cannot_be_brought_back_into_a_new_encounter():
    services, router, keeper, kardel = await _solo_room()
    await router.dispatch(keeper, ".npc create hive_scum | Культист")
    await router.dispatch(keeper, ".npc create hive_scum | Громила")
    await router.dispatch(keeper, ".combat start Культист")
    await _shoot_until_defeated(services, keeper, kardel, "Культист")

    refused = await router.dispatch(keeper, ".combat start Культист")
    assert refused == get_i18n("ru").t("combat.command.start.defeated")
    assert await load_encounter(services, CHAT) is None
    # A fresh opponent opens a new fight, and the old aftermath is cleared.
    assert not (await router.dispatch(keeper, ".combat start Громила")).startswith("❌")
    assert await services.store.state_get(CHAT, COMBAT_AFTERMATH_KEY) is None


async def test_the_fight_goes_on_while_any_opponent_still_stands():
    services, router, keeper, kardel = await _solo_room()
    await router.dispatch(keeper, ".npc create hive_scum | Культист")
    await router.dispatch(keeper, ".npc create hive_scum | Громила")
    await router.dispatch(keeper, ".combat start Культист, Громила")
    final = await _shoot_until_defeated(services, keeper, kardel, "Культист")
    assert final["encounter_ended"] is False
    state = await load_encounter(services, CHAT)
    assert state.combatants["Культист"].defeated and not state.combatants["Громила"].defeated
    passed = await resolve_action(services, keeper, {"id": "after-first", "actor": "Кардел", "action": END_TURN_ACTION})
    assert passed["ok"] and passed["encounter_ended"] is False
    final = await _shoot_until_defeated(services, keeper, kardel, "Громила")
    assert final["encounter_ended"] is True and await load_encounter(services, CHAT) is None


async def test_an_allied_npc_fights_for_the_party_and_does_not_hold_the_fight_open():
    services, router, keeper, kardel = await _solo_room()
    await router.dispatch(keeper, ".npc create hive_scum | Культист")
    await router.dispatch(keeper, ".npc create hive_scum | Проводник")
    await router.dispatch(keeper, ".combat start Культист, +Проводник")
    state = await load_encounter(services, CHAT)
    assert state.combatants["Проводник"].side == PARTY_SIDE and state.combatants["Проводник"].controller == "keeper"
    assert state.combatants["Культист"].side == OPPOSITION_SIDE
    final = await _shoot_until_defeated(services, keeper, kardel, "Культист")
    assert final["encounter_ended"] is True


async def test_keeper_end_keeps_the_aftermath_and_the_prompt_reports_it():
    services, router, keeper, kardel = await _solo_room(locale="en")
    await router.dispatch(keeper, ".npc create hive_scum | Cultist")
    await router.dispatch(keeper, ".combat start Cultist")
    i18n = get_i18n("en")
    during = await inject_game_state_prompt(keeper, services.characters, services.store, i18n)
    assert i18n.t("prompt.game_state.encounter_contract") in during
    assert await router.dispatch(keeper, ".combat end") == i18n.t("combat.command.ended")
    aftermath = json.loads(await services.store.state_get(CHAT, COMBAT_AFTERMATH_KEY))
    assert aftermath["reason"] == "ended_by_keeper"
    after = await inject_game_state_prompt(keeper, services.characters, services.store, i18n)
    assert i18n.t("prompt.game_state.aftermath_contract") in after
    assert "Cultist" in after and i18n.t("prompt.game_state.encounter_contract") not in after


def test_an_already_decided_encounter_row_concludes_on_the_next_commit():
    """The live-play shape: every opponent defeated, one PC left passing turns alone."""
    state = CombatState(5, "Кардел", {
        "Кардел": CombatantState(2, 2, 1, 1, controller="keeper-1"),
        "Культист": CombatantState(2, 2, 1, 1, controller="keeper", defeated=True),
    }, order=["Кардел", "Культист"])
    assert encounter_concluded(state)
    state.pending_reaction = {"id": "p"}
    assert not encounter_concluded(state)  # never while an attack waits on a reaction


async def test_a_legacy_stuck_encounter_ends_on_the_next_action():
    services, router, keeper, _kardel = await _solo_room()
    await router.dispatch(keeper, ".npc create hive_scum | Культист")
    await start_room_encounter(services, keeper, ["Культист"])
    raw = json.loads(await services.store.state_get(CHAT, COMBAT_STATE_KEY))
    raw["combatants"]["Культист"]["defeated"] = True
    for combatant in raw["combatants"].values():
        combatant.pop("side")  # stored before sides existed
    raw["current_actor"] = "Кардел"
    await services.store.state_set(CHAT, COMBAT_STATE_KEY, json.dumps(raw, ensure_ascii=False))
    passed = await resolve_action(services, keeper, {"id": "stuck", "actor": "Кардел", "action": END_TURN_ACTION})
    assert passed["ok"] and passed["encounter_ended"] is True
    assert should_narrate(passed)  # the close of the fight is narrated even on a turn pass
    assert await load_encounter(services, CHAT) is None


async def test_keeper_check_tool_refuses_attack_checks_only_while_a_fight_is_open():
    services, router, keeper, _kardel = await _solo_room(locale="en")
    tools = DiceTools(services)
    await router.dispatch(keeper, ".npc create hive_scum | Cultist")
    await router.dispatch(keeper, ".combat start Cultist")
    refused = await tools.skill_check(keeper, skill_name="WS")
    assert refused.startswith("⚔️") and "combat controls" in refused
    assert (await tools.skill_check(keeper, skill_name="BS")).startswith("⚔️")
    assert not (await tools.skill_check(keeper, skill_name="Per")).startswith("⚔️")  # not an attack value
    await router.dispatch(keeper, ".combat end")
    assert not (await tools.skill_check(keeper, skill_name="WS")).startswith("⚔️")
