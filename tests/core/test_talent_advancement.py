import pytest

from core.advancement_purchase import (
    AdvancementPurchaseError,
    advancement_budget,
    initialize_advancement_budget,
)
from core.advancement_surface import available_advancement_surface, safe_purchase_advancement
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.talent_advancement import (
    load_talent_catalog,
    purchase_talent,
    quote_talent_purchase,
)


def _character(*aptitudes: str) -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    character = CharacterSheet("Acolyte", "dh2")
    character.aptitudes = list(aptitudes)
    initialize_advancement_budget(pack, character)
    return pack, character


def test_dh2_talent_catalog_contains_verified_purchasable_slice():
    pack = load_rulepack("dh2")
    catalog = load_talent_catalog(pack)

    assert catalog is not None
    assert catalog.field == "Talents"
    assert len(catalog.talents) == 64
    assert "Быстрая Перезарядка" in catalog.talents
    assert "Амбидекстрия" in catalog.talents
    assert "Враг" not in catalog.talents
    assert "Крепкое Телосложение" not in catalog.talents


def test_talent_quote_never_initializes_a_missing_budget():
    pack = load_rulepack("dh2")
    character = CharacterSheet("Legacy", "dh2")
    character.aptitudes = ["Ловкость", "Полевое"]

    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Быстрая Перезарядка")

    assert "__advancement__" not in character.secondary_attributes


def test_tier_one_talent_uses_its_two_declared_aptitudes():
    pack, character = _character("Ловкость", "Полевое")

    quote = quote_talent_purchase(pack, character, "Быстрая Перезарядка")

    assert quote.category == "talent"
    assert quote.stage == "tier_1"
    assert quote.aptitude_matches == 2
    assert quote.cost == 200
    assert quote.available_xp == 1000


def test_talent_purchase_appends_talent_and_spends_xp_atomically():
    pack, character = _character("Ловкость", "Полевое")

    result = purchase_talent(pack, character, "Быстрая Перезарядка")

    assert character.talents == ["Быстрая Перезарядка"]
    assert result.remaining_xp == 800
    budget = advancement_budget(pack, character)
    assert budget is not None
    assert (budget.available_xp, budget.spent_xp) == (800, 200)

    with pytest.raises(AdvancementPurchaseError):
        purchase_talent(pack, character, "Быстрая Перезарядка")
    assert character.talents == ["Быстрая Перезарядка"]
    assert advancement_budget(pack, character) == budget


def test_fixed_specialization_is_required_and_validated():
    pack, character = _character("Общая", "Изящество")

    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Выучка с Оружием")
    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Выучка с Оружием (Невыдуманное)")

    result = purchase_talent(pack, character, "Выучка с Оружием (Лазерное)")

    assert result.quote.cost == 200
    assert character.talents == ["Выучка с Оружием (Лазерное)"]


def test_free_specializations_are_distinct_purchases():
    pack, character = _character("Выносливость", "Защита")

    first = purchase_talent(pack, character, "Сопротивление (Страх)")
    second = purchase_talent(pack, character, "Сопротивление::Токсины")

    assert first.quote.cost == 200
    assert second.quote.cost == 200
    assert character.talents == ["Сопротивление (Страх)", "Сопротивление (Токсины)"]

    with pytest.raises(AdvancementPurchaseError):
        purchase_talent(pack, character, "Сопротивление (страх)")


def test_specialization_can_override_talent_aptitudes():
    pack, character = _character("Навык Стрельбы", "Изящество")

    ranged = quote_talent_purchase(pack, character, "Обоерукий воин (Стрелковое)")
    melee = quote_talent_purchase(pack, character, "Обоерукий воин (Рукопашное)")

    assert ranged.stage == "tier_2"
    assert ranged.aptitude_matches == 2
    assert ranged.cost == 300
    assert melee.aptitude_matches == 1
    assert melee.cost == 450


def test_generic_advancement_surface_routes_talent_purchases():
    pack, character = _character("Ловкость", "Полевое")

    surface = available_advancement_surface(pack, character)
    assert surface is not None
    assert any(
        quote.category == "talent" and quote.target == "Быстрая Перезарядка"
        for quote in surface.purchases
    )

    result = safe_purchase_advancement(pack, character, "talent", "Быстрая Перезарядка")
    assert result.quote.category == "talent"
    assert "Быстрая Перезарядка" in character.talents
