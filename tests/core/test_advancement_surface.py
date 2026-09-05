import copy

import pytest

from core.advancement_purchase import AdvancementPurchaseError, initialize_advancement_budget
from core.advancement_surface import (
    available_advancement_surface,
    initialized_advancement_budget,
    safe_next_advancement_purchase,
    safe_purchase_advancement,
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


def test_surface_does_not_silently_grant_starting_xp_to_uninitialized_sheet():
    pack = _pack()
    character = _character()

    before = copy.deepcopy(character.secondary_attributes)
    assert initialized_advancement_budget(pack, character) is None
    assert available_advancement_surface(pack, character) is None
    assert character.secondary_attributes == before

    with pytest.raises(AdvancementPurchaseError):
        safe_next_advancement_purchase(pack, character, "characteristic", "Ag")
    assert character.secondary_attributes == before


def test_surface_rejects_bare_special_skill_family_without_mutation():
    pack = _pack()
    character = _character(aptitudes=["Интеллект", "Полевое"])
    initialize_advancement_budget(pack, character)
    before = copy.deepcopy(vars(character))

    with pytest.raises(AdvancementPurchaseError):
        safe_purchase_advancement(pack, character, "skill", "Навигация")

    assert vars(character) == before
    assert "Navigation" not in character.skills


def test_surface_accepts_explicit_specialization():
    pack = _pack()
    character = _character(aptitudes=["Интеллект", "Полевое"])
    initialize_advancement_budget(pack, character)

    result = safe_purchase_advancement(pack, character, "skill", "Навигация (варп)")

    assert result.quote.target == "Navigation::варп"
    assert character.skills["Navigation::варп"] == 1
    assert result.remaining_xp == 900


def test_available_surface_lists_regular_targets_and_only_existing_specializations():
    pack = _pack()
    character = _character(aptitudes=["Ловкость", "Изящество", "Интеллект", "Полевое"])
    character.skills["Navigation::варп"] = 1
    initialize_advancement_budget(pack, character)

    surface = available_advancement_surface(pack, character)

    assert surface is not None and surface.budget.available_xp == 1000
    keys = {(quote.category, quote.target) for quote in surface.purchases}
    assert ("characteristic", "Ag") in keys
    assert ("skill", "Acrobatics") in keys
    assert ("skill", "Navigation::варп") in keys
    assert ("skill", "Navigation") not in keys
