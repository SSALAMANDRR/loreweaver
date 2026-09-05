import copy

import pytest

from core.advancement import AdvancementError
from core.advancement_purchase import (
    AdvancementPurchaseError,
    advancement_budget,
    initialize_advancement_budget,
    load_advancement_purchase_spec,
    next_advancement_purchase,
    purchase_advancement,
)
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack


def _pack():
    return load_rulepack("dh2")


def _character(*, aptitudes=()):
    character = CharacterSheet("Acolyte", "dh2")
    character.aptitudes = list(aptitudes)
    character.attributes["Ag"] = 30
    character.attributes["Int"] = 30
    return character


def test_dh2_purchase_sidecar_declares_characteristic_and_skill_progressions():
    spec = load_advancement_purchase_spec(_pack())
    assert spec is not None
    assert spec.progressions["characteristic"].stages == (
        "simple",
        "intermediate",
        "trained",
        "proficient",
        "expert",
    )
    assert spec.progressions["characteristic"].stage_source == "history"
    assert spec.progressions["characteristic"].step == 5
    assert spec.progressions["skill"].stages == ("known", "trained", "experienced", "veteran")
    assert spec.progressions["skill"].stage_source == "value"
    assert spec.progressions["skill"].step == 1


def test_initial_budget_is_1000_xp_and_reinitialization_is_idempotent():
    pack = _pack()
    character = _character()

    budget = initialize_advancement_budget(pack, character)
    assert budget is not None
    assert (budget.starting_xp, budget.available_xp, budget.spent_xp) == (1000, 1000, 0)

    state = copy.deepcopy(character.secondary_attributes["__advancement__"])
    again = initialize_advancement_budget(pack, character)
    assert again == budget
    assert character.secondary_attributes["__advancement__"] == state


def test_characteristic_purchases_use_history_for_sequential_plus_five_stages():
    pack = _pack()
    character = _character(aptitudes=["Ловкость", "Изящество"])
    initialize_advancement_budget(pack, character)

    first = purchase_advancement(pack, character, "characteristic", "Ag")
    assert first.quote.stage == "simple"
    assert first.quote.cost == 100
    assert character.attributes["Ag"] == 35
    assert (first.remaining_xp, first.spent_xp) == (900, 100)

    second = purchase_advancement(pack, character, "characteristic", "Ловкость")
    assert second.quote.stage == "intermediate"
    assert second.quote.cost == 250
    assert character.attributes["Ag"] == 40
    assert (second.remaining_xp, second.spent_xp) == (650, 350)


def test_skill_purchase_uses_current_rank_so_creation_grants_are_not_rebought():
    pack = _pack()
    character = _character(aptitudes=["Ловкость"])
    character.skills["Acrobatics"] = 1
    initialize_advancement_budget(pack, character)

    quote = next_advancement_purchase(pack, character, "skill", "Акробатика")
    assert quote.stage == "trained"
    assert quote.current_value == 1
    assert quote.next_value == 2
    # Agility + automatic General aptitude = two matches.
    assert quote.cost == 200

    result = purchase_advancement(pack, character, "skill", "Acrobatics")
    assert character.skills["Acrobatics"] == 2
    assert result.remaining_xp == 800


def test_untrained_skill_buys_known_then_trained_in_order():
    pack = _pack()
    character = _character(aptitudes=["Ловкость"])
    initialize_advancement_budget(pack, character)

    first = purchase_advancement(pack, character, "skill", "Acrobatics")
    second = purchase_advancement(pack, character, "skill", "Acrobatics")

    assert first.quote.stage == "known" and first.quote.cost == 100
    assert second.quote.stage == "trained" and second.quote.cost == 200
    assert character.skills["Acrobatics"] == 2
    assert advancement_budget(pack, character).available_xp == 700


def test_specialized_skill_uses_family_requirement_and_persists_its_own_rank():
    pack = _pack()
    character = _character(aptitudes=["Интеллект", "Полевое"])
    initialize_advancement_budget(pack, character)

    result = purchase_advancement(pack, character, "skill", "Навигация (варп)")

    assert result.quote.target == "Navigation::варп"
    assert result.quote.stage == "known"
    assert result.quote.cost == 100
    assert character.skills["Navigation::варп"] == 1


def test_influence_cannot_be_purchased_because_it_has_no_advancement_requirement():
    pack = _pack()
    character = _character()
    initialize_advancement_budget(pack, character)

    with pytest.raises(AdvancementError):
        next_advancement_purchase(pack, character, "characteristic", "Inf")


def test_insufficient_xp_fails_without_mutating_character():
    pack = _pack()
    character = _character(aptitudes=["Ловкость", "Изящество"])
    initialize_advancement_budget(pack, character)
    character.secondary_attributes["__advancement__"]["available"] = 50
    before = copy.deepcopy(vars(character))

    with pytest.raises(AdvancementPurchaseError):
        purchase_advancement(pack, character, "characteristic", "Ag")

    assert vars(character) == before


def test_characteristic_has_exactly_five_purchase_stages():
    pack = _pack()
    character = _character(aptitudes=["Ловкость", "Изящество"])
    initialize_advancement_budget(pack, character)
    # Ignore budget in this structural test so we can walk all five stages.
    character.secondary_attributes["__advancement__"]["available"] = 10000

    stages = [purchase_advancement(pack, character, "characteristic", "Ag").quote.stage for _ in range(5)]
    assert stages == ["simple", "intermediate", "trained", "proficient", "expert"]
    assert character.attributes["Ag"] == 55

    with pytest.raises(AdvancementPurchaseError):
        next_advancement_purchase(pack, character, "characteristic", "Ag")


def test_advancement_state_round_trips_with_generic_character_serialization():
    pack = _pack()
    character = _character(aptitudes=["Ловкость", "Изящество"])
    initialize_advancement_budget(pack, character)
    purchase_advancement(pack, character, "characteristic", "Ag")

    restored = CharacterSheet.from_dict(character.to_dict())
    budget = advancement_budget(pack, restored)

    assert budget is not None
    assert (budget.available_xp, budget.spent_xp) == (900, 100)
    assert restored.secondary_attributes["__advancement__"]["history"][0]["target"] == "Ag"
