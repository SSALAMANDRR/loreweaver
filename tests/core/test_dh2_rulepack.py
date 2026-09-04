from __future__ import annotations

from core.check_outcome import RollDetail
from core.rulepacks import load_rulepack


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


def _degrees(roll: int, target: int) -> int:
    """Independent transcription of CH01_H022.

    Positive values are degrees of success, negative values are degrees of
    failure.  A check always starts at one degree on whichever side it lands.
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


def test_dh2_characteristic_bonuses_are_tens_digits():
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

    assert derived == {
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
    }


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
