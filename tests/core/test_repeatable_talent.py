import pytest

from core.advancement_purchase import AdvancementPurchaseError, advancement_budget, initialize_advancement_budget
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.sheets import sheet_value
from core.talent_advancement import purchase_talent, quote_talent_purchase


def _character() -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    character = CharacterSheet("Acolyte", "dh2")
    character.aptitudes = ["Выносливость"]
    character.attributes["T"] = 20
    character.attributes["WOUNDS"] = 10
    initialize_advancement_budget(pack, character)
    return pack, character


def test_toughness_talent_is_repeatable_indexed_and_adds_one_wound_each_time():
    pack, character = _character()

    first_quote = quote_talent_purchase(pack, character, "Крепкое Телосложение")
    assert (first_quote.current_value, first_quote.next_value) == (0, 1)
    assert first_quote.stage == "tier_1"
    assert first_quote.cost == 200

    first = purchase_talent(pack, character, "Крепкое Телосложение")
    second = purchase_talent(pack, character, "Крепкое Телосложение")

    assert first.quote.next_value == 1
    assert second.quote.next_value == 2
    assert character.talents == ["Крепкое Телосложение (1)", "Крепкое Телосложение (2)"]
    assert sheet_value(character, pack, "Wounds") == 12
    budget = advancement_budget(pack, character)
    assert budget is not None
    assert (budget.available_xp, budget.spent_xp) == (600, 400)


def test_toughness_talent_cap_is_twice_current_toughness_bonus_and_failure_is_atomic():
    pack, character = _character()

    for _ in range(4):
        purchase_talent(pack, character, "Крепкое Телосложение")

    assert character.talents == [
        "Крепкое Телосложение (1)",
        "Крепкое Телосложение (2)",
        "Крепкое Телосложение (3)",
        "Крепкое Телосложение (4)",
    ]
    assert sheet_value(character, pack, "Wounds") == 14
    before_sheet = character.to_dict()
    before_budget = advancement_budget(pack, character)

    with pytest.raises(AdvancementPurchaseError, match="purchase limit"):
        quote_talent_purchase(pack, character, "Крепкое Телосложение")
    with pytest.raises(AdvancementPurchaseError, match="purchase limit"):
        purchase_talent(pack, character, "Крепкое Телосложение")

    assert character.to_dict() == before_sheet
    assert advancement_budget(pack, character) == before_budget
