from __future__ import annotations

import pytest

from core.character_manager import CharacterSheet
from core.check_outcome import RollDetail
from core.rulepacks import load_rulepack
from core.sheets import (
    UntrainedSkillError,
    check_value,
    has_check_value,
    refresh_sheet,
    resolve_skill_family,
    set_sheet_value,
    sheet_value,
    wire_resources,
)


DIFFICULTY_DELTAS = {
    None: 0,
    "trivial": 60,
    "elementary": 50,
    "simple": 40,
    "easy": 30,
    "routine": 20,
    "ordinary": 10,
    "challenging": 0,
    "difficult": -10,
    "hard": -20,
    "very_hard": -30,
    "strenuous": -40,
    "punishing": -50,
    "hellish": -60,
}


REGULAR_SKILLS = {
    "Acrobatics": ("Ag", ["Акробатика"]),
    "Athletics": ("S", ["Атлетика"]),
    "Awareness": ("Per", ["Бдительность"]),
    "Security": ("Int", ["Безопасность"]),
    "Survival": ("Per", ["Выживание"]),
    "Inquiry": ("Fel", ["Дознание"]),
    "Interrogation": ("WP", ["Допрос"]),
    "Intimidate": ("S", ["Запугивание"]),
    "Command": ("Fel", ["Командование"]),
    "Commerce": ("Int", ["Коммерция"]),
    "SleightOfHand": ("Ag", ["Ловкость Рук", "Ловкость рук"]),
    "Logic": ("Int", ["Логика"]),
    "Medicae": ("Int", ["Медика"]),
    "Charm": ("Fel", ["Обаяние"]),
    "Deceive": ("Fel", ["Обман"]),
    "Parry": ("WS", ["Парирование"]),
    "Scrutiny": ("Per", ["Проницательность"]),
    "Psyniscience": ("Per", ["Психонаука"]),
    "Stealth": ("Ag", ["Скрытность"]),
    "TechUse": ("Int", ["Техпользование", "Тех-пользование"]),
    "Dodge": ("Ag", ["Уклонение"]),
}


SPECIAL_SKILL_FAMILIES = {
    "ForbiddenLore": ("Int", "Запретные Знания"),
    "Linguistics": ("Int", "Лингвистика"),
    "Navigation": ("Int", "Навигация"),
    "CommonLore": ("Int", "Общие Знания"),
    "Trade": ("Int", "Ремесло"),
    "Operate": ("Ag", "Управление"),
    "ScholasticLore": ("Int", "Учёные Знания"),
}


def _degrees(roll: int, target: int) -> int:
    """Independent transcription of CH01_H022."""
    if roll <= target:
        return 1 + (target - roll) // 10
    return -(1 + (roll - target) // 10)


def test_dh2_names_and_russian_aliases_resolve():
    pack = load_rulepack("dh2")

    assert load_rulepack("dark heresy 2e") is pack
    assert load_rulepack("тёмная ересь 2") is pack
    assert pack.resolve_skill("НР") == "WS"
    assert pack.resolve_skill("Навык Стрельбы") == "BS"
    assert pack.resolve_skill("СВ") == "WP"
    assert pack.resolve_skill("Влияние") == "Inf"
    assert pack.resolve_skill("Раны") == "Wounds"
    assert pack.resolve_skill("Усталость") == "Fatigue"


def test_dh2_characteristic_bonuses_and_fatigue_threshold():
    pack = load_rulepack("dh2")
    derived = pack.compute_derived(
        {
            "WS": 43,
            "BS": 57,
            "S": 42,
            "T": 39,
            "Ag": 61,
            "Int": 50,
            "Per": 28,
            "WP": 77,
            "Fel": 31,
            "Inf": 99,
        }
    )

    expected = {
        "WSB": 4,
        "BSB": 5,
        "SB": 4,
        "TB": 3,
        "AgB": 6,
        "IntB": 5,
        "PerB": 2,
        "WPB": 7,
        "FelB": 3,
        "InfB": 9,
        "FatigueThreshold": 10,
    }
    assert {key: derived[key] for key in expected} == expected


def test_dh2_difficulty_table_matches_neon_source():
    resolver = load_rulepack("dh2").resolver

    for raw_target in range(1, 101):
        for difficulty, delta in DIFFICULTY_DELTAS.items():
            assert resolver.effective_target(raw_target, difficulty=difficulty) == raw_target + delta


def test_dh2_percentile_check_and_degrees_match_rulebook_exhaustively():
    resolver = load_rulepack("dh2").resolver

    for raw_target in range(1, 101):
        for difficulty, delta in DIFFICULTY_DELTAS.items():
            effective = raw_target + delta
            for roll in range(1, 101):
                outcome = resolver.interpret(
                    RollDetail("1d100", (roll,), roll),
                    raw_target,
                    difficulty=difficulty,
                )
                assert outcome.rank.id == ("success" if roll <= effective else "fail")
                assert outcome.rank.success == (roll <= effective)
                assert outcome.margin == _degrees(roll, effective)


def test_dh2_degree_boundaries_start_at_one_and_step_each_full_ten():
    resolver = load_rulepack("dh2").resolver

    expected = {
        50: 1,
        49: 1,
        41: 1,
        40: 2,
        31: 2,
        30: 3,
        51: -1,
        59: -1,
        60: -2,
        69: -2,
        70: -3,
    }
    for roll, degrees in expected.items():
        outcome = resolver.interpret(RollDetail("1d100", (roll,), roll), 50)
        assert outcome.margin == degrees


def test_dh2_initiative_declares_d10_plus_agility_bonus():
    pack = load_rulepack("dh2")

    assert pack.initiative_roll == "1d10 + {AgB}"


def test_dh2_sheet_tracks_damage_and_fatigue_as_upward_counters():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes.update(
        {
            "T": 43,
            "WP": 39,
            "WOUNDS": 12,
            "DAMAGE": 7,
            "FATIGUE": 3,
        }
    )

    refresh_sheet(sheet, pack, preserve_trained=False)

    assert sheet_value(sheet, pack, "Wounds") == 12
    assert sheet_value(sheet, pack, "Damage") == 7
    assert sheet_value(sheet, pack, "Fatigue") == 3
    assert sheet_value(sheet, pack, "FatigueThreshold") == 7

    meters = {entry["id"]: entry for entry in wire_resources(sheet, pack, "ru")}
    assert meters["damage"] == {"id": "damage", "label": "Урон", "value": 7, "max": 12}
    assert meters["fatigue"] == {"id": "fatigue", "label": "Усталость", "value": 3, "max": 7}

    sheet.attributes["DAMAGE"] = 15
    sheet.attributes["FATIGUE"] = 9
    meters = {entry["id"]: entry for entry in wire_resources(sheet, pack, "en")}
    assert meters["damage"]["value"] == 15 and meters["damage"]["max"] == 12
    assert meters["fatigue"]["value"] == 9 and meters["fatigue"]["max"] == 7


def test_dh2_regular_skill_aliases_resolve_and_family_names_do_not_collapse():
    pack = load_rulepack("dh2")

    for canonical, (_, aliases) in REGULAR_SKILLS.items():
        for alias in aliases:
            assert pack.resolve_skill(alias) == canonical

    # A family name by itself remains non-rollable. The specialization is part
    # of the skill identity and is resolved by the generic sheet-family layer.
    for family in SPECIAL_SKILL_FAMILIES:
        assert pack.resolve_skill(family) is None


def test_dh2_regular_skill_training_levels_feed_the_check_target():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes["Ag"] = 43

    expected = {0: 23, 1: 43, 2: 53, 3: 63, 4: 73}
    for rank, target in expected.items():
        sheet.skills["Acrobatics"] = rank
        assert check_value(sheet, pack, "Acrobatics") == target

        canonical = pack.resolve_skill("Акробатика")
        assert canonical == "Acrobatics"
        assert check_value(sheet, pack, canonical) == target


def test_dh2_regular_skill_base_bindings_and_rank_constraints_match_table_3_3():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes.update({"WS": 44, "S": 51, "Ag": 43, "Int": 48, "Per": 37, "WP": 46, "Fel": 39})
    sheet.skills.update(
        {
            "Athletics": 1,
            "Awareness": 2,
            "Interrogation": 3,
            "Command": 4,
            "Parry": 1,
            "Dodge": 0,
        }
    )

    assert check_value(sheet, pack, "Athletics") == 51
    assert check_value(sheet, pack, "Awareness") == 47
    assert check_value(sheet, pack, "Interrogation") == 66
    assert check_value(sheet, pack, "Command") == 69
    assert check_value(sheet, pack, "Parry") == 44
    assert check_value(sheet, pack, "Dodge") == 23
    assert sheet.skills["Command"] == 4

    for canonical in REGULAR_SKILLS:
        assert canonical in pack.sheet_spec.skill_keys
        assert canonical in pack.sheet_spec.check_values

    for canonical in SPECIAL_SKILL_FAMILIES:
        assert canonical not in pack.sheet_spec.skill_keys
        assert canonical not in pack.sheet_spec.check_values
        assert canonical in pack.sheet_spec.skill_families


def test_dh2_special_skill_families_match_table_3_3_and_forbid_untrained_checks():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes.update({"Int": 48, "Ag": 43})

    for family_id, (base, russian_name) in SPECIAL_SKILL_FAMILIES.items():
        family = pack.sheet_spec.skill_families[family_id]
        assert family.base == base
        assert family.ranks == {1: 0, 2: 10, 3: 20, 4: 30}
        assert family.untrained_modifier is None

        resolved = resolve_skill_family(pack, f"{russian_name} (Тест)")
        assert resolved is not None
        canonical, resolved_family, _spec, specialization = resolved
        assert resolved_family == family_id
        assert canonical == f"{family_id}::тест"
        assert specialization == "тест"
        assert not has_check_value(sheet, pack, f"{russian_name} (Тест)")
        with pytest.raises(UntrainedSkillError):
            check_value(sheet, pack, f"{russian_name} (Тест)")


def test_dh2_special_skill_specializations_are_independent_and_use_the_training_ladder():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes.update({"Int": 48, "Ag": 43})

    set_sheet_value(sheet, pack, "Навигация (Варп)", 2)
    set_sheet_value(sheet, pack, "Навигация (Наземная)", 4)
    set_sheet_value(sheet, pack, "Управление (Наземная)", 3)

    assert sheet.skills["Navigation::варп"] == 2
    assert sheet.skills["Navigation::наземная"] == 4
    assert sheet.skills["Operate::наземная"] == 3

    assert check_value(sheet, pack, "Навигация (Варп)") == 58
    assert check_value(sheet, pack, "Navigation::ВАРП") == 58
    assert check_value(sheet, pack, "Навигация (Наземная)") == 78
    assert check_value(sheet, pack, "Управление (Наземная)") == 63

    assert has_check_value(sheet, pack, "Навигация (Варп)")
    assert has_check_value(sheet, pack, "Навигация (Наземная)")
    assert not has_check_value(sheet, pack, "Навигация (Звёздная)")
