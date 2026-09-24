"""Defeat lifecycle. Damage above Wounds is Critical Damage (CH07_H099, CH07_H101); a
Troop NPC is killed or taken out of action by any Critical Damage (Глава XII с. 469,
«Эффектная Гибель»). PCs and Elite/Master NPCs need the Critical Effect tables, which
are not implemented, so they are never marked defeated."""

import copy
import json
from dataclasses import asdict

import pytest

from core.character_manager import CharacterSheet
from core.combat import (
    DECLINE_REACTION,
    KEEPER_CONTROLLER,
    ActionRequest,
    CombatValidationError,
    EncounterCombatant,
    apply_combat_state_delta,
    apply_reaction_delta,
    apply_state_delta,
    project_combat_result,
    project_combat_state,
    resolve_end_turn,
    resolve_first_shot,
    resolve_melee_attack,
    resolve_reaction,
    start_encounter,
)
from core.documents import PLAYER_VIEWER
from core.item_model import ItemInstance, load_item_catalog
from core.npc_profiles import load_npc_profiles, materialize_npc_sheet
from core.rulepacks import load_rulepack
from tests.core.test_combat_encounter import ScriptedDice


def _pc(pack, name="Ada"):
    catalog = load_item_catalog(pack)
    sheet = CharacterSheet(name, "dh2")
    sheet.attributes.update({"BS": 100, "WS": 100, "S": 40, "T": 30, "Ag": 45, "WOUNDS": 10, "DAMAGE": 0})
    sheet.equipment = [ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 60})]
    return sheet


def _scene(npc_damage=9, extra_troop=False):
    pack = load_rulepack("dh2")
    profiles = load_npc_profiles(pack)
    pc = _pc(pack)
    scum = materialize_npc_sheet(profiles["hive_scum"], "Scum", pack)
    scum.attributes["DAMAGE"] = npc_damage  # Wounds 9: the next damage is Critical Damage
    members = [EncounterCombatant(pc, "u1"), EncounterCombatant(scum, KEEPER_CONTROLLER)]
    totals = [20, 5]
    if extra_troop:
        members.append(EncounterCombatant(materialize_npc_sheet(profiles["hired_gun"], "Gun", pack), KEEPER_CONTROLLER))
        totals.append(1)
    state, _ = start_encounter(members, pack=pack, dice=ScriptedDice(totals))
    return pack, pc, scum, state


def _lasgun(sheet):
    return next(item.instance_id for item in sheet.equipment if item.profile_id == "lasgun")


def _shoot_and_decline(pack, pc, target, state, damage=5, attack_roll=5):
    request = ActionRequest(pc, target, _lasgun(pc), attack_roll=attack_roll)
    pending = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    apply_state_delta(request, pending, combat_state=state, pack=pack)
    final = resolve_reaction(
        pending_id="p", choice=DECLINE_REACTION, attacker=pc, defender=target, combat_state=state, pack=pack,
        damage_rolls=(damage,),
    )
    assert final.ok, final.validation_failure
    return final


def test_critical_damage_takes_a_troop_out_of_the_fight_in_the_same_delta():
    pack, pc, scum, state = _scene()
    final = _shoot_and_decline(pack, pc, scum, state)
    assert final.target_defeated is True
    assert final.state_delta.combat_state_after.combatants["Scum"].defeated is True
    assert state.combatants["Scum"].defeated is False  # nothing applied yet
    apply_reaction_delta(scum, final, combat_state=state, pack=pack)
    assert state.combatants["Scum"].defeated is True and state.pending_reaction is None
    assert scum.attributes["DAMAGE"] > scum.attributes["WOUNDS"]


def test_damage_up_to_wounds_is_not_critical_and_does_not_defeat():
    pack, pc, scum, state = _scene(npc_damage=0)
    # Roll 99 vs 110: 2 degrees, below the die face 3, so no substitution: 6 - TB 2 = 4.
    final = _shoot_and_decline(pack, pc, scum, state, damage=6, attack_roll=99)
    assert final.final_damage == 4 and final.target_defeated is False


def test_a_player_character_above_wounds_is_never_auto_defeated():
    pack, pc, scum, state = _scene()
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    pc.attributes["DAMAGE"] = 10
    catalog = load_item_catalog(pack)
    knife = next(item.instance_id for item in scum.equipment if item.profile_id == "knife")
    scum.attributes["WS"] = 100
    request = ActionRequest(scum, pc, knife, attack_roll=5)
    pending = resolve_melee_attack(request, combat_state=state, pack=pack, reaction_window=True, pending_id="n")
    apply_state_delta(request, pending, combat_state=state, pack=pack)
    final = resolve_reaction(
        pending_id="n", choice=DECLINE_REACTION, attacker=scum, defender=pc, combat_state=state, pack=pack,
        damage_rolls=(5,),
    )
    assert final.final_damage > 0 and final.target_defeated is False
    assert catalog is not None


def test_turn_order_skips_a_defeated_combatant_and_wraps_rounds():
    pack, pc, scum, state = _scene(extra_troop=True)
    assert state.order == ["Ada", "Scum", "Gun"]
    apply_reaction_delta(scum, _shoot_and_decline(pack, pc, scum, state), combat_state=state, pack=pack)
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.current_actor, state.round_number) == ("Gun", 1)
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.current_actor, state.round_number) == ("Ada", 2)


def test_when_only_the_current_combatant_remains_its_next_turn_is_the_next_round():
    pack, pc, scum, state = _scene()
    apply_reaction_delta(scum, _shoot_and_decline(pack, pc, scum, state), combat_state=state, pack=pack)
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.current_actor, state.round_number) == ("Ada", 2)
    assert state.combatants["Ada"].action_budget == 2


def test_the_last_combatant_in_order_defeated_wraps_to_the_top():
    pack, pc, scum, state = _scene(extra_troop=True)
    gun = materialize_npc_sheet(load_npc_profiles(pack)["hired_gun"], "Gun", pack)
    gun.attributes["DAMAGE"] = 10
    apply_reaction_delta(gun, _shoot_and_decline(pack, pc, gun, state, damage=9), combat_state=state, pack=pack)
    assert state.combatants["Gun"].defeated
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.current_actor, state.round_number) == ("Ada", 2)


def test_a_defeated_combatant_cannot_be_targeted_and_nothing_mutates():
    pack, pc, scum, state = _scene()
    apply_reaction_delta(scum, _shoot_and_decline(pack, pc, scum, state), combat_state=state, pack=pack)
    before = copy.deepcopy(state)
    ammo = next(item.current_ammo for item in pc.equipment)
    request = ActionRequest(pc, scum, _lasgun(pc), attack_roll=5, mode="semi")
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="q")
    assert result.validation_failure == "target is out of the fight"
    assert state == before and next(item.current_ammo for item in pc.equipment) == ammo


def test_a_stale_apply_leaves_no_partial_defeat():
    pack, pc, scum, state = _scene()
    request = ActionRequest(pc, scum, _lasgun(pc), attack_roll=5)
    pending = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    apply_state_delta(request, pending, combat_state=state, pack=pack)
    final = resolve_reaction(
        pending_id="p", choice=DECLINE_REACTION, attacker=pc, defender=scum, combat_state=state, pack=pack,
        damage_rolls=(5,),
    )
    scum.attributes["DAMAGE"] = 3  # someone else changed the sheet meanwhile
    before = copy.deepcopy(state)
    with pytest.raises(CombatValidationError, match="stale"):
        apply_reaction_delta(scum, final, combat_state=state, pack=pack)
    assert state == before and state.combatants["Scum"].defeated is False
    assert scum.attributes["DAMAGE"] == 3


def test_defeat_is_public_but_npc_internals_stay_private():
    pack, pc, scum, state = _scene()
    final = _shoot_and_decline(pack, pc, scum, state)
    apply_reaction_delta(scum, final, combat_state=state, pack=pack)
    view = project_combat_state(state, PLAYER_VIEWER)
    assert next(entry for entry in view["order"] if entry["name"] == "Scum")["defeated"] is True
    assert "Scum" not in view["combatants"]
    public = project_combat_result(asdict(final), state, PLAYER_VIEWER)
    assert public["target_defeated"] is True
    assert public["state_delta"]["target_damage_after"] is None
    assert "combat_state_after" not in json.dumps(public)
