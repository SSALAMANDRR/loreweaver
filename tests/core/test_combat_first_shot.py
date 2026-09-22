import copy

import pytest

from core.character_manager import CharacterSheet
from core.combat import (
    ActionRequest,
    apply_combat_state_delta,
    apply_state_delta,
    create_combat_state,
    resolve_first_shot,
    resolve_turn_transition,
)
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack


def _scene(ammo=3):
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    attacker = CharacterSheet("attacker", "dh2")
    target = CharacterSheet("target", "dh2")
    attacker.attributes["BS"] = 50
    target.attributes.update({"T": 40, "DAMAGE": 0})
    weapon = ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": ammo})
    attacker.equipment = [weapon]
    target.equipment = [ItemInstance.create(catalog.resolve("basic_flak_armor"))]
    combat_state = create_combat_state(
        [attacker.name, target.name], current_actor=attacker.name, pack=pack
    )
    return pack, attacker, target, weapon, combat_state


def _reset_attacker_turn(pack, combat_state, *, new_round=False):
    if not new_round:
        transition = resolve_turn_transition(combat_state, next_actor="target", pack=pack)
        apply_combat_state_delta(combat_state, transition)
    transition = resolve_turn_transition(
        combat_state,
        next_actor="attacker",
        round_number=combat_state.round_number + 1 if new_round else None,
        pack=pack,
    )
    apply_combat_state_delta(combat_state, transition)


def test_initial_combat_state_has_pack_declared_action_and_reaction_budget():
    _, _, _, _, combat_state = _scene()

    assert combat_state.round_number == 1
    assert combat_state.current_actor == "attacker"
    assert combat_state.combatants["attacker"].action_budget == 1
    assert combat_state.combatants["target"].reactions_remaining == 1


def test_first_shot_hit_resolves_location_armour_tb_damage_and_state_atomically():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
    )
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)

    assert result.success is True
    assert result.hit_location == "body"
    assert result.armour_before == 3 and result.armour_after_penetration == 3
    assert result.tb_reduction == 4 and result.final_damage == 3
    assert result.state_delta.action_cost == 1
    assert target.attributes["DAMAGE"] == 0 and weapon.current_ammo == 3
    assert combat_state.combatants["attacker"].action_budget == 1

    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 3 and weapon.current_ammo == 2
    assert combat_state.combatants["attacker"].action_budget == 0


def test_apply_rolls_back_ammo_and_combat_state_if_entity_mutation_fails():
    class RejectDamageWrite(dict):
        def __setitem__(self, key, value):
            if key == "DAMAGE":
                raise RuntimeError("injected damage write failure")
            super().__setitem__(key, value)

    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
    )
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)
    before = copy.deepcopy(combat_state)
    target.attributes = RejectDamageWrite(target.attributes)

    with pytest.raises(RuntimeError, match="injected damage write failure"):
        apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert weapon.current_ammo == 3
    assert target.attributes["DAMAGE"] == 0
    assert combat_state == before


def test_first_shot_miss_still_consumes_action_and_ammo_but_not_damage():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=90)
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)

    assert result.success is False and result.final_damage == 0
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 0 and weapon.current_ammo == 2
    assert combat_state.combatants["attacker"].action_budget == 0


def test_insufficient_action_budget_rejects_without_partial_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    combat_state.combatants[attacker.name].action_budget = 0
    before = copy.deepcopy(combat_state)

    result = resolve_first_shot(
        ActionRequest(attacker, target, weapon.instance_id, attack_roll=20),
        combat_state=combat_state,
        pack=pack,
    )

    assert result.validation_failure == "insufficient action budget"
    assert combat_state == before
    assert weapon.current_ammo == 3 and target.attributes["DAMAGE"] == 0


def test_actor_without_current_turn_is_rejected_without_partial_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    combat_state.current_actor = target.name
    before = copy.deepcopy(combat_state)

    result = resolve_first_shot(
        ActionRequest(attacker, target, weapon.instance_id, attack_roll=20),
        combat_state=combat_state,
        pack=pack,
    )

    assert result.validation_failure == "actor does not have the current turn"
    assert combat_state == before
    assert weapon.current_ammo == 3 and target.attributes["DAMAGE"] == 0


def test_first_shot_validation_failure_is_non_mutating():
    pack, attacker, target, weapon, combat_state = _scene(ammo=0)
    before = copy.deepcopy(combat_state)

    result = resolve_first_shot(
        ActionRequest(attacker, target, weapon.instance_id),
        combat_state=combat_state,
        pack=pack,
    )

    assert result.validation_failure == "out of ammunition"
    assert combat_state == before
    assert weapon.current_ammo == 0 and target.attributes["DAMAGE"] == 0


def test_missing_weapon_instance_is_rejected_without_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    before = copy.deepcopy(combat_state)

    result = resolve_first_shot(
        ActionRequest(attacker, target, "missing"), combat_state=combat_state, pack=pack
    )

    assert result.validation_failure == "weapon instance is missing"
    assert combat_state == before
    assert weapon.current_ammo == 3 and target.attributes["DAMAGE"] == 0


def test_successful_basic_reaction_prevents_hit_and_is_consumed():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
        reaction_roll=10,
        reaction_target=30,
    )
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)

    assert result.success is False
    assert result.reaction == {
        "type": "basic",
        "roll": 10,
        "target": 30,
        "success": True,
        "prevents_hit": True,
        "spent": True,
    }
    assert result.state_delta.reaction_cost == 1
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 0 and weapon.current_ammo == 2
    assert combat_state.combatants[target.name].reactions_remaining == 0


def test_failed_basic_reaction_continues_damage_and_is_consumed():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
        reaction_roll=90,
        reaction_target=30,
    )
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)

    assert result.success is True and result.reaction["prevents_hit"] is False
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 3 and weapon.current_ammo == 2
    assert combat_state.combatants[target.name].reactions_remaining == 0


def test_second_reaction_in_same_round_is_rejected_without_partial_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    first = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        reaction_roll=90,
        reaction_target=30,
        damage_roll=7,
    )
    first_result = resolve_first_shot(first, combat_state=combat_state, pack=pack)
    apply_state_delta(first, first_result, combat_state=combat_state, pack=pack)
    _reset_attacker_turn(pack, combat_state)
    before = copy.deepcopy(combat_state)
    ammo_before = weapon.current_ammo
    damage_before = target.attributes["DAMAGE"]

    second = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        reaction_roll=10,
        reaction_target=30,
        damage_roll=10,
    )
    result = resolve_first_shot(second, combat_state=combat_state, pack=pack)

    assert result.validation_failure == "reaction is unavailable"
    assert combat_state == before
    assert weapon.current_ammo == ammo_before
    assert target.attributes["DAMAGE"] == damage_before


def test_new_round_restores_reaction_and_current_actor_action_budget():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        reaction_roll=90,
        reaction_target=30,
        damage_roll=7,
    )
    result = resolve_first_shot(request, combat_state=combat_state, pack=pack)
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    transition = resolve_turn_transition(
        combat_state, next_actor=attacker.name, round_number=2, pack=pack
    )
    assert combat_state.round_number == 1
    assert combat_state.combatants[target.name].reactions_remaining == 0
    apply_combat_state_delta(combat_state, transition)

    assert combat_state.round_number == 2
    assert combat_state.current_actor == attacker.name
    assert combat_state.combatants[attacker.name].action_budget == 1
    assert combat_state.combatants[target.name].reactions_remaining == 1
