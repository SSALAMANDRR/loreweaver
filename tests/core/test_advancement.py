from __future__ import annotations

import pytest

from core.advancement import (
    AdvancementError,
    advancement_requirements,
    count_matching_aptitudes,
    load_advancement_spec,
    quote_advancement,
)
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack


EXPECTED_DH2_COSTS = {
    "characteristic": {
        "simple": {0: 500, 1: 250, 2: 100},
        "intermediate": {0: 750, 1: 500, 2: 250},
        "trained": {0: 1000, 1: 750, 2: 500},
        "proficient": {0: 1500, 1: 1000, 2: 750},
        "expert": {0: 2500, 1: 1500, 2: 1250},
    },
    "skill": {
        "known": {0: 300, 1: 200, 2: 100},
        "trained": {0: 600, 1: 400, 2: 200},
        "experienced": {0: 900, 1: 600, 2: 300},
        "veteran": {0: 1200, 1: 800, 2: 400},
    },
    "talent": {
        "tier_1": {0: 600, 1: 300, 2: 200},
        "tier_2": {0: 900, 1: 450, 2: 300},
        "tier_3": {0: 1200, 1: 600, 2: 400},
    },
}


def test_dh2_advancement_sidecar_matches_step4_cost_tables():
    spec = load_advancement_spec(load_rulepack("dh2"))

    assert spec is not None
    assert spec.starting_xp == 1000
    assert spec.aptitude_field == "Aptitudes"
    assert spec.base_aptitudes == ("Общая",)
    assert spec.costs == EXPECTED_DH2_COSTS


def test_dh2_characteristic_requirements_match_table_2_3():
    spec = load_advancement_spec(load_rulepack("dh2"))
    assert spec is not None

    assert advancement_requirements(spec, "characteristic", "WS") == ("Навык Рукопашной", "Нападение")
    assert advancement_requirements(spec, "characteristic", "Ag") == ("Ловкость", "Изящество")
    assert advancement_requirements(spec, "characteristic", "Fel") == ("Общительность", "Общение")
    with pytest.raises(AdvancementError, match="unknown advancement target"):
        advancement_requirements(spec, "characteristic", "Inf")


def test_dh2_characteristic_quote_uses_zero_one_or_two_matching_aptitudes():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")
    required = ["Выносливость", "Защита"]

    sheet.aptitudes = ["Познание", "Техно"]
    assert quote_advancement(pack, sheet, "characteristic", "expert", required).cost == 2500

    sheet.aptitudes = ["Выносливость", "Техно"]
    one = quote_advancement(pack, sheet, "characteristic", "expert", required)
    assert (one.aptitude_matches, one.cost) == (1, 1500)

    sheet.aptitudes = ["Выносливость", "Защита"]
    two = quote_advancement(pack, sheet, "characteristic", "expert", required)
    assert (two.aptitude_matches, two.cost) == (2, 1250)


def test_dh2_target_quotes_resolve_characteristics_regular_and_specialized_skills():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    sheet.aptitudes = ["Ловкость", "Изящество"]
    agility = quote_advancement(pack, sheet, "characteristic", "simple", target="Ag")
    assert (agility.aptitude_matches, agility.cost) == (2, 100)

    # General is a universal aptitude in the sidecar, so Acrobatics gets both
    # matches even if the sheet only explicitly stores Agility here.
    sheet.aptitudes = ["Ловкость"]
    acrobatics = quote_advancement(pack, sheet, "skill", "known", target="Acrobatics")
    assert (acrobatics.aptitude_matches, acrobatics.cost) == (2, 100)

    sheet.aptitudes = ["Интеллект", "Полевое"]
    navigation = quote_advancement(pack, sheet, "skill", "trained", target="Navigation::варп")
    assert (navigation.aptitude_matches, navigation.cost) == (2, 200)


def test_dh2_skill_and_talent_quotes_use_their_own_tables():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")
    sheet.aptitudes = ["Ловкость", "Полевое"]

    skill = quote_advancement(pack, sheet, "skill", "known", ["Ловкость", "Полевое"])
    talent = quote_advancement(pack, sheet, "talent", "tier_3", ["Ловкость", "Полевое"])

    assert (skill.aptitude_matches, skill.cost) == (2, 100)
    assert (talent.aptitude_matches, talent.cost) == (2, 400)


def test_aptitude_matching_is_case_insensitive_and_does_not_double_count_duplicates():
    assert count_matching_aptitudes(
        [" Защита ", "ЗАЩИТА", "Выносливость"],
        ["защита", "Нападение"],
    ) == 1


def test_advancement_quote_rejects_unknown_stage_instead_of_guessing():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(AdvancementError, match="unknown advancement stage"):
        quote_advancement(pack, sheet, "skill", "legendary", ["Интеллект", "Познание"])


def test_advancement_quote_requires_exactly_one_requirement_source():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(AdvancementError, match="exactly one"):
        quote_advancement(pack, sheet, "skill", "known")
    with pytest.raises(AdvancementError, match="exactly one"):
        quote_advancement(pack, sheet, "skill", "known", ["Ловкость", "Общая"], target="Acrobatics")
