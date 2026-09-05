import pytest

from core.advancement_purchase import AdvancementPurchaseError, advancement_budget, initialize_advancement_budget
from core.advancement_surface import available_advancement_surface, safe_next_advancement_purchase, safe_purchase_advancement
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.talent_advancement import load_talent_catalog, purchase_talent, quote_talent_purchase
from core.talent_requirements import (
    load_talent_requirements,
    talent_requirements_met,
)


_NO_PREREQUISITE_TALENTS = {
    "Быстрая Перезарядка",
    "Выучка с Оружием",
    "Выучка с Экзотическим Оружием",
    "Выхватить Оружие",
    "Крепкое Телосложение",
    "Неистовство",
    "Ненависть",
    "Нокдаун",
    "Обоерукий воин",
    "Плечом к Плечу",
    "Сопротивление",
    "Спринт",
}


def _character(*aptitudes: str) -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    character = CharacterSheet("Acolyte", "dh2")
    character.aptitudes = list(aptitudes)
    initialize_advancement_budget(pack, character)
    return pack, character


def test_dh2_catalog_has_no_unstructured_prerequisite_talents():
    pack = load_rulepack("dh2")
    catalog = load_talent_catalog(pack)
    requirements = load_talent_requirements(pack)

    assert catalog is not None
    assert requirements is not None
    assert len(catalog.talents) == 84
    assert len(requirements.requirements) == 72
    assert set(catalog.talents) == set(requirements.requirements) | _NO_PREREQUISITE_TALENTS
    assert "Враг" not in catalog.talents
    assert "Крепкое Телосложение" in catalog.talents
    assert "Мастер Клинка" in catalog.talents


def test_characteristic_requirement_hides_and_blocks_talent_until_met():
    pack, character = _character("Навык Рукопашной", "Навык Стрельбы")
    character.attributes["Ag"] = 29

    assert not talent_requirements_met(pack, character, "Амбидекстрия")
    surface = available_advancement_surface(pack, character)
    assert surface is not None
    assert not any(q.target == "Амбидекстрия" for q in surface.purchases)

    before = advancement_budget(pack, character)
    with pytest.raises(AdvancementPurchaseError):
        safe_purchase_advancement(pack, character, "talent", "Амбидекстрия")
    assert character.talents == []
    assert advancement_budget(pack, character) == before

    character.attributes["Ag"] = 30
    assert talent_requirements_met(pack, character, "Амбидекстрия")
    quote = safe_next_advancement_purchase(pack, character, "talent", "Амбидекстрия")
    assert quote.stage == "tier_1"
    result = safe_purchase_advancement(pack, character, "talent", "Амбидекстрия")
    assert result.quote.target == "Амбидекстрия"
    assert "Амбидекстрия" in character.talents


def test_skill_rank_and_nested_any_requirements_are_evaluated():
    pack, character = _character("Восприятие", "Защита")
    character.skills["Awareness"] = 1
    character.attributes["Int"] = 35
    character.attributes["Per"] = 34

    assert not talent_requirements_met(pack, character, "Постоянная бдительность::Интеллект")

    character.skills["Awareness"] = 2
    assert talent_requirements_met(pack, character, "Постоянная бдительность::Интеллект")

    character.attributes["Int"] = 34
    assert not talent_requirements_met(pack, character, "Постоянная бдительность::Интеллект")

    character.attributes["Per"] = 35
    assert talent_requirements_met(pack, character, "Постоянная бдительность::Восприятие")


def test_talent_requirement_can_accept_any_owned_specialization():
    pack, character = _character("Изящество", "Нападение")

    assert not talent_requirements_met(pack, character, "Двойной Выстрел")

    character.talents.append("Обоерукий воин (Стрелковое)")
    assert talent_requirements_met(pack, character, "Двойной Выстрел")


def test_exact_talent_specialization_requirement_is_enforced():
    pack, character = _character("Сила Воли", "Защита")
    character.attributes["WP"] = 45
    character.talents.extend(["Искушённый", "Сопротивление (Токсины)"])

    assert not talent_requirements_met(pack, character, "Несокрушимая вера")

    character.talents.append("Сопротивление (Страх)")
    assert talent_requirements_met(pack, character, "Несокрушимая вера")


def test_special_skill_requirement_reads_family_specialization_rank():
    pack, character = _character("Интеллект", "Техно")
    character.attributes["Int"] = 35
    character.skills["TechUse"] = 1
    character.skills["Trade::оружейник"] = 0

    assert not talent_requirements_met(pack, character, "Мастер Брони")

    character.skills["Trade::оружейник"] = 1
    assert talent_requirements_met(pack, character, "Мастер Брони")


def test_requirement_failure_never_spends_xp_or_mutates_talents():
    pack, character = _character("Навык Рукопашной", "Изящество")
    character.attributes["WS"] = 29
    before_budget = advancement_budget(pack, character)
    before_talents = list(character.talents)

    with pytest.raises(AdvancementPurchaseError):
        safe_purchase_advancement(pack, character, "talent", "Быстрая Атака")

    assert character.talents == before_talents
    assert advancement_budget(pack, character) == before_budget


def test_low_level_talent_api_cannot_bypass_prerequisites():
    pack, character = _character("Навык Рукопашной", "Изящество")
    character.attributes["WS"] = 29
    before_budget = advancement_budget(pack, character)
    before_talents = list(character.talents)

    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Быстрая Атака")
    with pytest.raises(AdvancementPurchaseError):
        purchase_talent(pack, character, "Быстрая Атака")

    assert character.talents == before_talents
    assert advancement_budget(pack, character) == before_budget


def test_mechanicus_trait_unlocks_mechadendrite_purchase():
    pack, character = _character("Интеллект", "Техно")

    assert not talent_requirements_met(pack, character, "Использование Механодендритов::Боевые")
    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Использование Механодендритов (Боевые)")

    character.traits.append("Имплантаты Механикус")
    assert talent_requirements_met(pack, character, "Использование Механодендритов::Боевые")
    quote = quote_talent_purchase(pack, character, "Использование Механодендритов (Боевые)")
    assert quote.stage == "tier_2"
    assert quote.cost == 300


def test_legacy_mechadendrite_specialization_alias_blocks_duplicate_purchase():
    pack, character = _character("Интеллект", "Техно")
    character.traits.append("Имплантаты Механикус")
    character.talents.append("Использование Механодендритов (Вспомогательный)")
    before_budget = advancement_budget(pack, character)

    with pytest.raises(AdvancementPurchaseError):
        quote_talent_purchase(pack, character, "Использование Механодендритов (Вспомогательные)")
    with pytest.raises(AdvancementPurchaseError):
        purchase_talent(pack, character, "Использование Механодендритов::Вспомогательные")

    assert character.talents == ["Использование Механодендритов (Вспомогательный)"]
    assert advancement_budget(pack, character) == before_budget


def test_unyielding_requires_psychic_resistance_specialization():
    pack, character = _character("Сила Воли", "Защита")
    character.attributes["WP"] = 30
    character.talents.append("Сопротивление (Страх)")

    assert not talent_requirements_met(pack, character, "Непреклонный")

    character.talents.append("Сопротивление (Психические Силы)")
    assert talent_requirements_met(pack, character, "Непреклонный")


def test_specialization_dependent_requirement_uses_selected_attack_mode():
    pack, character = _character("Навык Стрельбы", "Изящество")
    character.attributes["WS"] = 39
    character.attributes["BS"] = 40
    character.attributes["Per"] = 35

    assert not talent_requirements_met(pack, character, "Неотразимая Атака::Рукопашная")
    assert talent_requirements_met(pack, character, "Неотразимая Атака::Стрелковая")

    ranged = quote_talent_purchase(pack, character, "Неотразимая Атака (Стрелковая)")
    assert ranged.stage == "tier_2"
    assert ranged.aptitude_matches == 2
    assert ranged.cost == 300


def test_selected_skill_requirement_tracks_mastery_specialization():
    pack, character = _character("Интеллект", "Познание")
    character.skills["Awareness"] = 4
    character.skills["Logic"] = 3

    assert talent_requirements_met(pack, character, "Мастерство::Awareness")
    assert not talent_requirements_met(pack, character, "Мастерство::Logic")

    quote = quote_talent_purchase(pack, character, "Мастерство (Awareness)")
    assert quote.stage == "tier_3"
    assert quote.cost == 400


def test_any_lore_specialization_satisfies_infused_knowledge():
    pack, character = _character("Интеллект", "Познание")
    character.attributes["Int"] = 40

    assert not talent_requirements_met(pack, character, "Вложенное Знание")

    character.skills["CommonLore::Империум"] = 1
    assert talent_requirements_met(pack, character, "Вложенное Знание")


def test_psy_rating_presence_is_grounded_in_psyker_elite_advance():
    pack, character = _character("Восприятие", "Псайкер")
    character.attributes["Per"] = 30
    character.skills["Psyniscience"] = 1

    assert not talent_requirements_met(pack, character, "Варп-чувство")

    character.elite_advances.append("Псайкер")
    assert talent_requirements_met(pack, character, "Варп-чувство")


def test_cybernetic_prerequisites_use_equipment_plus_mechanicus_trait():
    pack, character = _character("Навык Рукопашной", "Техно")
    character.equipment.append("Конденсатор люминена")

    assert not talent_requirements_met(pack, character, "Шок Люминена")

    character.traits.append("Имплантаты Механикус")
    assert talent_requirements_met(pack, character, "Шок Люминена")


def test_two_weapon_master_requires_both_wielder_specializations():
    pack, character = _character("Нападение", "Изящество")
    character.attributes["Ag"] = 45
    character.attributes["WS"] = 40
    character.talents.extend(["Амбидекстрия", "Обоерукий воин (Рукопашное)"])

    assert not talent_requirements_met(pack, character, "Обоерукий мастер")

    character.talents.append("Обоерукий воин (Стрелковое)")
    assert talent_requirements_met(pack, character, "Обоерукий мастер")


def test_blademaster_requires_ws30_and_melee_weapon_training():
    pack, character = _character("Навык Рукопашной", "Изящество")
    character.attributes["WS"] = 30

    character.talents.append("Выучка с Оружием (Лазерное)")
    assert not talent_requirements_met(pack, character, "Мастер Клинка")

    character.talents.append("Выучка с Оружием (Цепное)")
    assert talent_requirements_met(pack, character, "Мастер Клинка")
    quote = quote_talent_purchase(pack, character, "Мастер Клинка")
    assert quote.stage == "tier_3"
    assert quote.cost == 400


def test_blademaster_accepts_existing_combined_low_tech_creation_grant():
    pack, character = _character("Навык Рукопашной", "Изящество")
    character.attributes["WS"] = 30
    character.talents.append("Выучка с Оружием (Лазерное, Низкотехнологичное)")

    assert talent_requirements_met(pack, character, "Мастер Клинка")
