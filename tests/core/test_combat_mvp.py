import copy
from dataclasses import replace

import pytest

from core.character_manager import CharacterSheet
from core.combat import (
    ActionRequest,
    CombatValidationError,
    apply_combat_state_delta,
    apply_state_delta,
    create_combat_state,
    resolve_aim,
    resolve_first_shot,
    resolve_melee_attack,
    resolve_reload,
    resolve_turn_transition,
)
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack


def _scene(profile_id="autogun", ammo=20):
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    attacker = CharacterSheet("attacker", "dh2")
    target = CharacterSheet("target", "dh2")
    attacker.attributes.update({"BS": 50, "WS": 50, "S": 40})
    target.attributes.update({"Ag": 40, "Dodge": 1, "T": 40, "DAMAGE": 0})
    weapon = ItemInstance.create(catalog.resolve(profile_id), state={"current_ammo": ammo})
    attacker.equipment = [weapon]
    target.equipment = [ItemInstance.create(catalog.resolve("basic_flak_armor"))]
    state = create_combat_state([attacker.name, target.name], current_actor=attacker.name, pack=pack)
    return pack, attacker, target, weapon, state


def test_hit_location_reverses_attack_roll_without_location_input():
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=23, damage_roll=10)
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert result.success and result.hit_location == "body"  # 23 -> 32
    assert result.hits[0].location == "body"
    assert result.hits[0].armour_before == 3


def test_range_modifier_is_pack_declared_and_out_of_range_rejects():
    pack, attacker, target, weapon, state = _scene()
    near = ActionRequest(attacker, target, weapon.instance_id, attack_roll=50, distance=2, damage_roll=7)
    assert resolve_first_shot(near, combat_state=state, pack=pack).attack_target == 90  # BS 50 + Standard +10 + point blank +30
    distant = ActionRequest(attacker, target, weapon.instance_id, attack_roll=20, distance=500)
    before = copy.deepcopy(state)
    rejected = resolve_first_shot(distant, combat_state=state, pack=pack)
    assert rejected.validation_failure == "target is out of weapon range"
    assert state == before and weapon.current_ammo == 20


@pytest.mark.parametrize(("roll", "damage"), [(20, 0), (90, 3)])
def test_dodge_uses_reaction_state_and_controls_damage(roll, damage):
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(
        attacker, target, weapon.instance_id, attack_roll=23, damage_roll=10,
        reaction_type="dodge", reaction_roll=roll,
    )
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert result.reaction["target"] == 40
    assert result.reaction["spent"] is True
    assert result.final_damage == damage
    apply_state_delta(request, result, combat_state=state, pack=pack)
    assert target.attributes["DAMAGE"] == damage
    assert state.combatants[target.name].reactions_remaining == 0

    # The attacker still has one half action, but a second Standard Attack in the
    # same turn is not a *different* half action (CH07_H019); reaction exhaustion
    # itself is covered by test_second_reaction_in_same_round_is_rejected_*.
    second = ActionRequest(
        attacker, target, weapon.instance_id, attack_roll=23,
        reaction_type="dodge", reaction_roll=10,
    )
    before = copy.deepcopy(state)
    rejected = resolve_first_shot(second, combat_state=state, pack=pack)
    assert rejected.validation_failure == "action was already taken this turn"
    assert state == before


def test_half_aim_then_half_attack_consumes_whole_budget_and_bonus_once():
    pack, attacker, target, weapon, state = _scene()
    aim = ActionRequest(attacker, None, weapon.instance_id, mode="half")
    aim_result = resolve_aim(aim, combat_state=state, pack=pack)
    assert aim_result.ok and aim_result.state_delta.action_cost == 1
    apply_state_delta(aim, aim_result, combat_state=state, pack=pack)
    assert state.combatants[attacker.name].action_budget == 1

    shot = ActionRequest(attacker, target, weapon.instance_id, attack_roll=55, damage_roll=8)
    result = resolve_first_shot(shot, combat_state=state, pack=pack)
    assert result.attack_target == 70 and result.success  # BS 50 + Standard +10 + half Aim +10
    apply_state_delta(shot, result, combat_state=state, pack=pack)
    assert state.combatants[attacker.name].action_budget == 0
    assert state.combatants[attacker.name].aim_bonus == 0
    rejected = resolve_first_shot(shot, combat_state=state, pack=pack)
    assert rejected.validation_failure == "insufficient action budget"


def test_full_aim_persists_until_next_turn_attack():
    pack, attacker, target, weapon, state = _scene()
    aim = ActionRequest(attacker, None, weapon.instance_id, mode="full")
    result = resolve_aim(aim, combat_state=state, pack=pack)
    apply_state_delta(aim, result, combat_state=state, pack=pack)
    assert state.combatants[attacker.name].action_budget == 0
    transition = resolve_turn_transition(state, next_actor="target", pack=pack)
    apply_combat_state_delta(state, transition)
    transition = resolve_turn_transition(state, next_actor="attacker", round_number=2, pack=pack)
    apply_combat_state_delta(state, transition)
    shot = ActionRequest(attacker, target, weapon.instance_id, attack_roll=65, damage_roll=8)
    assert resolve_first_shot(shot, combat_state=state, pack=pack).attack_target == 80  # BS 50 + Standard +10 + full Aim +20


def test_reload_refills_clip_and_uses_full_action():
    pack, attacker, target, weapon, state = _scene(ammo=3)
    request = ActionRequest(attacker, None, weapon.instance_id, mode="full")
    result = resolve_reload(request, combat_state=state, pack=pack)
    assert result.ok and result.ammo_before == 3 and result.ammo_after == 30
    assert result.state_delta.action_cost == 2
    apply_state_delta(request, result, combat_state=state)
    assert weapon.current_ammo == 30
    assert state.combatants[attacker.name].action_budget == 0


def test_reload_validation_failure_and_stale_ammo_leave_state_unchanged():
    pack, attacker, _, weapon, state = _scene(ammo=30)
    request = ActionRequest(attacker, None, weapon.instance_id, mode="full")
    before = copy.deepcopy(state)
    rejected = resolve_reload(request, combat_state=state, pack=pack)
    assert rejected.validation_failure == "weapon clip is already full"
    assert state == before and weapon.current_ammo == 30
    weapon.state["current_ammo"] = 3
    result = resolve_reload(request, combat_state=state, pack=pack)
    weapon.state["current_ammo"] = 2
    with pytest.raises(CombatValidationError, match="stale weapon state"):
        apply_state_delta(request, result, combat_state=state, pack=pack)
    assert state == before and weapon.current_ammo == 2


def test_reload_rejects_insufficient_full_action_budget():
    pack, attacker, target, weapon, state = _scene(ammo=3)
    shot = ActionRequest(attacker, target, weapon.instance_id, attack_roll=90)
    apply_state_delta(shot, resolve_first_shot(shot, combat_state=state, pack=pack), combat_state=state, pack=pack)
    before = copy.deepcopy(state)
    request = ActionRequest(attacker, None, weapon.instance_id, mode="full")
    rejected = resolve_reload(request, combat_state=state, pack=pack)
    assert rejected.validation_failure == "insufficient action budget"
    assert state == before and weapon.current_ammo == 2


@pytest.mark.parametrize(
    ("mode", "roll", "shots", "hits", "budget"),
    # Short and Long Burst are half actions (CH07_H048, CH07_H035): one half action remains.
    [("single", 20, 1, 1, 1), ("semi", 20, 3, 2, 1), ("full", 10, 10, 4, 1)],
)
def test_rate_of_fire_spends_ammo_and_resolves_each_hit(mode, roll, shots, hits, budget):
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(
        attacker, target, weapon.instance_id, mode=mode, attack_roll=roll,
        damage_rolls=(10,) * hits,
    )
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert result.ok and result.shots_fired == shots
    assert len(result.hits) == hits
    assert sum(hit.final_damage for hit in result.hits) == result.final_damage
    assert result.ammo_after == 20 - shots
    apply_state_delta(request, result, combat_state=state, pack=pack)
    assert target.attributes["DAMAGE"] == result.final_damage
    assert weapon.current_ammo == 20 - shots
    assert state.combatants[attacker.name].action_budget == budget


def test_multi_hit_reduces_armour_and_tb_independently_and_applies_atomically():
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(
        attacker, target, weapon.instance_id, mode="semi", attack_roll=20,
        damage_rolls=(10, 10), location_rolls=(50, 10),
    )
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert [(hit.location, hit.armour_before, hit.tb_reduction, hit.final_damage)
            for hit in result.hits] == [("body", 3, 4, 3), ("head", 0, 4, 6)]
    assert result.final_damage == 9
    before = copy.deepcopy(state)
    target.attributes["DAMAGE"] = 1
    with pytest.raises(CombatValidationError, match="stale target damage state"):
        apply_state_delta(request, result, combat_state=state, pack=pack)
    assert state == before and weapon.current_ammo == 20
    target.attributes["DAMAGE"] = 0
    apply_state_delta(request, result, combat_state=state, pack=pack)
    assert target.attributes["DAMAGE"] == 9 and weapon.current_ammo == 17


def test_attacks_on_successive_turns_accumulate_canonical_damage():
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=23, damage_roll=10)
    for expected_damage in (3, 6):
        if expected_damage == 6:
            # Two Standard Attacks may not share one turn (CH07_H019): target's turn, then a new round.
            apply_combat_state_delta(state, resolve_turn_transition(state, next_actor=target.name, pack=pack))
            apply_combat_state_delta(
                state, resolve_turn_transition(state, next_actor=attacker.name, pack=pack, round_number=2)
            )
        result = resolve_first_shot(request, combat_state=state, pack=pack)
        assert result.state_delta.target_damage_after == expected_damage
        apply_state_delta(request, result, combat_state=state, pack=pack)
        assert target.attributes["DAMAGE"] == expected_damage
    assert state.combatants[attacker.name].action_budget == 1


def test_invalid_fixed_roll_rejects_without_mutation():
    pack, attacker, target, weapon, state = _scene()
    before = copy.deepcopy(state)
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=101)
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert result.validation_failure == "percentile roll must be between 1 and 100"
    assert state == before and weapon.current_ammo == 20 and target.attributes["DAMAGE"] == 0


def test_successful_dodge_removes_only_declared_number_of_burst_hits():
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(
        attacker, target, weapon.instance_id, mode="full", attack_roll=10,
        damage_rolls=(10, 10, 10), reaction_type="dodge", reaction_roll=40,
    )
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    assert result.reaction["hits_avoided"] == 1
    assert result.reaction["prevents_hit"] is False
    assert result.success and len(result.hits) == 3
    assert result.shots_fired == 10
    apply_state_delta(request, result, combat_state=state, pack=pack)
    assert state.combatants[target.name].reactions_remaining == 0


def test_invalid_ammunition_delta_is_rejected_before_mutation():
    pack, attacker, target, weapon, state = _scene()
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=20, damage_roll=10)
    result = resolve_first_shot(request, combat_state=state, pack=pack)
    invalid = replace(result, state_delta=replace(result.state_delta, ammo_after=999))
    before = copy.deepcopy(state)
    with pytest.raises(CombatValidationError, match="combat ammunition delta is invalid"):
        apply_state_delta(request, invalid, combat_state=state, pack=pack)
    assert state == before and weapon.current_ammo == 20 and target.attributes["DAMAGE"] == 0


def test_unsupported_fire_mode_and_incomplete_laspistol_are_non_mutating():
    pack, attacker, target, weapon, state = _scene(profile_id="lasgun")
    before = copy.deepcopy(state)
    request = ActionRequest(attacker, target, weapon.instance_id, mode="full", attack_roll=20)
    assert resolve_first_shot(request, combat_state=state, pack=pack).validation_failure
    assert state == before and weapon.current_ammo == 20
    catalog = load_item_catalog(pack)
    pistol = ItemInstance.create(catalog.resolve("laspistol"))
    attacker.equipment.append(pistol)
    request = ActionRequest(attacker, target, pistol.instance_id, attack_roll=20)
    assert resolve_first_shot(request, combat_state=state, pack=pack).validation_failure
    assert state == before and target.attributes["DAMAGE"] == 0


def test_melee_parry_regression_with_two_half_actions():
    pack, attacker, target, _, state = _scene()
    catalog = load_item_catalog(pack)
    sword = ItemInstance.create(catalog.resolve("sword"))
    attacker.equipment.append(sword)
    target.attributes.update({"WS": 40, "Parry": 1})
    request = ActionRequest(
        attacker, target, sword.instance_id, attack_roll=20, damage_roll=10,
        reaction_type="parry", reaction_roll=30,
    )
    result = resolve_melee_attack(request, combat_state=state, pack=pack)
    assert result.reaction["success"] and result.final_damage == 0
    apply_state_delta(request, result, combat_state=state, pack=pack)
    assert target.attributes["DAMAGE"] == 0
    assert state.combatants[target.name].reactions_remaining == 0
