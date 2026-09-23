import copy

import pytest

from core.character_manager import CharacterSheet
from core.combat import (
    ActionRequest,
    CombatValidationError,
    apply_combat_state_delta,
    apply_state_delta,
    create_combat_state,
    resolve_melee_attack,
    resolve_turn_transition,
)
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack


def _scene(*, weapon_profile="sword"):
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    attacker = CharacterSheet("attacker", "dh2")
    target = CharacterSheet("target", "dh2")
    attacker.attributes.update({"WS": 50, "S": 40})
    target.attributes.update({"WS": 40, "Parry": 1, "T": 40, "DAMAGE": 0})
    weapon = ItemInstance.create(catalog.resolve(weapon_profile))
    attacker.equipment = [weapon]
    target.equipment = [ItemInstance.create(catalog.resolve("basic_flak_armor"))]
    combat_state = create_combat_state(
        [attacker.name, target.name], current_actor=attacker.name, pack=pack
    )
    return pack, attacker, target, weapon, combat_state


def _next_attacker_turn(pack, combat_state):
    transition = resolve_turn_transition(combat_state, next_actor="target", pack=pack)
    apply_combat_state_delta(combat_state, transition)
    transition = resolve_turn_transition(combat_state, next_actor="attacker", pack=pack)
    apply_combat_state_delta(combat_state, transition)


@pytest.mark.parametrize(
    ("weapon_profile", "damage_roll", "expected_damage"),
    [("sword", 10, 7), ("knife", 5, 2)],
)
def test_successful_melee_hit_uses_ws_weapon_damage_armour_tb_and_action_budget(
    weapon_profile, damage_roll, expected_damage
):
    pack, attacker, target, weapon, combat_state = _scene(
        weapon_profile=weapon_profile
    )
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=damage_roll,
    )

    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)

    assert result.ok and result.action == "melee_attack"
    assert result.attack_target == 60 and result.attack_roll == 20  # WS 50 + Standard Attack +10
    assert result.success is True and result.hit_location == "body"
    assert result.raw_damage == damage_roll + 4
    assert result.penetration == 0
    assert result.armour_before == 3 and result.armour_after_penetration == 3
    assert result.tb_reduction == 4 and result.final_damage == expected_damage
    assert result.ammo_before is None and result.ammo_after is None
    assert result.state_delta.action_cost == 1
    assert target.attributes["DAMAGE"] == 0
    assert combat_state.combatants[attacker.name].action_budget == 2

    apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == expected_damage
    assert combat_state.combatants[attacker.name].action_budget == 1
    assert weapon.state == {}


def test_failed_ws_check_consumes_action_without_damage():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(attacker, target, weapon.instance_id, attack_roll=90)

    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)

    assert result.ok and result.attack_target == 60 and result.success is False
    assert result.final_damage == 0
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)
    assert target.attributes["DAMAGE"] == 0
    assert combat_state.combatants[attacker.name].action_budget == 1


def test_successful_parry_uses_existing_resolver_and_prevents_damage():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        damage_roll=10,
        reaction_roll=30,
        reaction_target=99,
    )

    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)

    assert result.success is False
    assert result.reaction == {
        "type": "parry",
        "roll": 30,
        "target": 40,
        "success": True,
        "prevents_hit": True,
        "spent": True,
    }
    assert result.state_delta.reaction_cost == 1
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)
    assert target.attributes["DAMAGE"] == 0
    assert combat_state.combatants[target.name].reactions_remaining == 0


def test_failed_parry_continues_damage_and_consumes_reaction():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
        reaction_roll=90,
    )

    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)

    assert result.success is True
    assert result.reaction["success"] is False
    assert result.reaction["prevents_hit"] is False
    apply_state_delta(request, result, combat_state=combat_state, pack=pack)
    assert target.attributes["DAMAGE"] == 7
    assert combat_state.combatants[target.name].reactions_remaining == 0


def test_second_parry_is_rejected_without_partial_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    first = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        damage_roll=7,
        reaction_roll=90,
    )
    first_result = resolve_melee_attack(first, combat_state=combat_state, pack=pack)
    apply_state_delta(first, first_result, combat_state=combat_state, pack=pack)
    _next_attacker_turn(pack, combat_state)
    before = copy.deepcopy(combat_state)
    damage_before = target.attributes["DAMAGE"]

    second = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        damage_roll=10,
        reaction_roll=10,
    )
    result = resolve_melee_attack(second, combat_state=combat_state, pack=pack)

    assert result.validation_failure == "reaction is unavailable"
    assert combat_state == before
    assert target.attributes["DAMAGE"] == damage_before
    assert weapon.state == {}


def test_melee_validation_failure_does_not_partially_mutate_state():
    pack, attacker, target, weapon, combat_state = _scene()
    before = copy.deepcopy(combat_state)
    request = ActionRequest(attacker, target, "missing", attack_roll=20)

    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)

    assert result.validation_failure == "weapon instance is missing"
    assert combat_state == before
    assert target.attributes["DAMAGE"] == 0
    assert weapon.state == {}


def test_melee_apply_rolls_back_damage_and_combat_state_on_write_failure():
    class RejectDamageWrite(dict):
        def __setitem__(self, key, value):
            if key == "DAMAGE":
                raise RuntimeError("injected melee damage write failure")
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
    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)
    before = copy.deepcopy(combat_state)
    target.attributes = RejectDamageWrite(target.attributes)

    with pytest.raises(RuntimeError, match="injected melee damage write failure"):
        apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 0
    assert combat_state == before
    assert weapon.state == {}


def test_ranged_weapon_is_not_accepted_as_a_melee_weapon():
    pack, attacker, target, _, combat_state = _scene()
    catalog = load_item_catalog(pack)
    lasgun = ItemInstance.create(
        catalog.resolve("lasgun"), state={"current_ammo": 3}
    )
    attacker.equipment.append(lasgun)
    before = copy.deepcopy(combat_state)

    result = resolve_melee_attack(
        ActionRequest(attacker, target, lasgun.instance_id, attack_roll=20),
        combat_state=combat_state,
        pack=pack,
    )

    assert result.validation_failure == "weapon does not support the declared action"
    assert lasgun.current_ammo == 3
    assert target.attributes["DAMAGE"] == 0
    assert combat_state == before


def test_stale_melee_delta_is_rejected_before_any_mutation():
    pack, attacker, target, weapon, combat_state = _scene()
    request = ActionRequest(
        attacker,
        target,
        weapon.instance_id,
        attack_roll=20,
        location_roll=50,
        damage_roll=10,
    )
    result = resolve_melee_attack(request, combat_state=combat_state, pack=pack)
    combat_state.combatants[attacker.name].action_budget = 0

    with pytest.raises(CombatValidationError, match="stale combat state"):
        apply_state_delta(request, result, combat_state=combat_state, pack=pack)

    assert target.attributes["DAMAGE"] == 0
    assert weapon.state == {}
