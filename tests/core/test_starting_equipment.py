import pytest

from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.starting_equipment import (
    StartingEquipmentError,
    available_starting_items,
    choose_starting_item,
    initialize_starting_equipment,
    load_starting_equipment_spec,
    starting_equipment_budget,
)


def _character(influence: int = 40) -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    character = CharacterSheet("Acolyte", "dh2")
    character.attributes["Inf"] = influence
    return pack, character


def test_dh2_starting_equipment_catalog_is_grounded_and_filters_by_availability():
    pack, character = _character()
    spec = load_starting_equipment_spec(pack)

    assert spec is not None
    assert spec.count_stat == "InfB"
    assert spec.minimum_availability == -10
    assert spec.weapon_magazines == 2
    assert len(spec.items) >= 40

    initialize_starting_equipment(pack, character)
    choices = {item.name for item in available_starting_items(pack, character)}
    assert "Огнемет" in choices
    assert "Лазган" in choices
    assert "Флак-жилет" in choices
    assert "Медпакет" in choices
    assert "Болтер" not in choices


def test_starting_allowance_is_explicitly_initialized_and_frozen_from_influence_bonus():
    pack, character = _character(47)

    assert starting_equipment_budget(pack, character) is None
    assert available_starting_items(pack, character) == ()
    with pytest.raises(StartingEquipmentError, match="not initialized"):
        choose_starting_item(pack, character, "Лазган")

    budget = initialize_starting_equipment(pack, character)
    assert budget is not None
    assert (budget.total, budget.used, budget.remaining) == (4, 0, 4)

    character.attributes["Inf"] = 99
    assert starting_equipment_budget(pack, character) == budget


def test_weapon_acquisition_consumes_one_slot_and_grants_two_standard_magazines():
    pack, character = _character(40)
    initialize_starting_equipment(pack, character)

    result = choose_starting_item(pack, character, "Lasgun")

    assert result.item.name == "Лазган"
    assert result.budget.remaining == 3
    assert result.equipment_added == (
        "Лазган",
        "Стандартные боеприпасы к Лазган (2 магазина)",
    )
    assert character.equipment[-2:] == list(result.equipment_added)


def test_too_rare_item_is_rejected_without_spending_a_slot_or_mutating_equipment():
    pack, character = _character(40)
    initialize_starting_equipment(pack, character)
    before_budget = starting_equipment_budget(pack, character)
    before_equipment = list(character.equipment)

    with pytest.raises(StartingEquipmentError, match="below the starting limit"):
        choose_starting_item(pack, character, "Болтер")

    assert character.equipment == before_equipment
    assert starting_equipment_budget(pack, character) == before_budget


def test_starting_acquisition_stops_exactly_when_frozen_allowance_is_spent():
    pack, character = _character(20)
    initialize_starting_equipment(pack, character)

    choose_starting_item(pack, character, "Одежда")
    choose_starting_item(pack, character, "Рекаф")
    before_equipment = list(character.equipment)
    before_budget = starting_equipment_budget(pack, character)

    assert before_budget is not None and before_budget.remaining == 0
    with pytest.raises(StartingEquipmentError, match="no starting-equipment selections remaining"):
        choose_starting_item(pack, character, "Наручники")

    assert character.equipment == before_equipment
    assert starting_equipment_budget(pack, character) == before_budget
