from __future__ import annotations

from core.character_manager import CharacterSheet
from core.check_outcome import RollDetail
from core.rulepacks import load_rulepack
from core.sheets import check_value, refresh_sheet, sheet_value, wire_resources


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
    "Acrobatics": "Ag",
    "Athletics": "S",
    "Awareness": "Per",
    "Security": "Int",
    "Survival": "Per",
    "Inquiry": "Fel",
    "Interrogation": "WP",
    "Intimidate": "S",
    "Command": "Fel",
    "Commerce": "Int",
    "SleightOfHand": "Ag",
    "Logic": "Int",
    "Medicae": "Int",
    "Charm": "Fel",
    "Deceive": "Fel",
    "Parry": "WS",
    "Scrutiny": "Per",
    "Psyniscience": "Per",
    "Stealth": "Ag",
    "TechUse": "Int",
    "Dodge": "Ag",
}

SPECIAL_SKILL_FAMILIES = (
    "Запретные Знания",
    "Лингвистика",
    "Навигация",
    "Общие Знания",
    "Ремесло",
    "Управление",
    "Учёные Знания",
)


def _degrees(roll: int, target: int) -> int:
    """Independent transcription of CH01_H022.

    Positive values are degrees of success, negative values are degrees of
    failure. A check always starts at one degree on whichever side it lands.
    """
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
    assert sheet_value(sheet, pack, "FatigueThreshold") == 7  # TB 4 + WPB 3

    meters = {entry["id"]: entry for entry in wire_resources(sheet, pack, "ru")}
    assert meters["damage"] == {"id": "damage", "label": "Урон", "value": 7, "max": 12}
    assert meters["fatigue"] == {"id": "fatigue", "label": "Усталость", "value": 3, "max": 7}

    # DH2 counters may exceed their thresholds; they are not Loreweaver vitals
    # and therefore must never be silently clamped current <= max.
    sheet.attributes["DAMAGE"] = 15
    sheet.attributes["FATIGUE"] = 9
    meters = {entry["id"]: entry for entry in wire_resources(sheet, pack, "en")}
    assert meters["damage"]["value"] == 15 and meters["damage"]["max"] == 12
    assert meters["fatigue"]["value"] == 9 and meters["fatigue"]["max"] == 7


def test_dh2_regular_skill_aliases_resolve_but_special_families_do_not_collapse():
    pack = load_rulepack("dh2")

    assert pack.resolve_skill("Акробатика") == "Acrobatics"
    assert pack.resolve_skill("Бдительность") == "Awareness"
    assert pack.resolve_skill("Ловкость рук") == "SleightOfHand"
    assert pack.resolve_skill("Парирование") == "Parry"
    assert pack.resolve_skill("Уклонение") == "Dodge"

    # CH03_H003: every specialization of a Special skill is a separate skill.
    # Until the generic specialization primitive exists, a family name must not
    # resolve to one fake shared score.
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
        assert check_value(sheet, pack, "Акробатика") == target


def test_dh2_regular_skill_base_bindings_and_rank_constraints_match_table_3_3():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "DH2")
    sheet.attributes.update({"WS": 44, "S": 51, "Ag": 43, "Int": 48, "Per": 37, "WP": 46, "Fel": 39})
    sheet.skills.update(
        {
            "Athletics": 1,
            "Awareness": 2,
            "Interrogation": 3,
            "Charm": 4,
            "Parry": 1,
            "TechUse": 2,
        }
    )

    assert check_value(sheet, pack, "Athletics") == 51
    assert check_value(sheet, pack, "Awareness") == 47
    assert check_value(sheet, pack, "Interrogation") == 66
    assert check_value(sheet, pack, "Charm") == 69
    assert check_value(sheet, pack, "Parry") == 44
    assert check_value(sheet, pack, "TechUse") == 58

    assert set(pack.sheet_spec.skills) == set(REGULAR_SKILLS)
    assert all(value == 0 for value in pack.sheet_spec.skills.values())
    assert pack.creation_constraints["skills"]["default"] == {"min": 0, "max": 4}
