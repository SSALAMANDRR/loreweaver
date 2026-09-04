from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.creation_profiles import (
    CreationProfileError,
    generate_profiled_character,
    resolve_creation_profile,
)
from core.dice_engine import seed_dice
from core.rulepacks import load_rulepack
from core.sheets import sheet_value, wire_resources


HOME_WORLDS = {
    "feral_world": {
        "alias": "Дикий мир",
        "positive": {"S", "T"},
        "negative": {"Inf"},
        "fate": 2,
        "blessing": 3,
    },
    "forge_world": {
        "alias": "Мир-кузница",
        "positive": {"Int", "T"},
        "negative": {"Fel"},
        "fate": 3,
        "blessing": 8,
    },
    "highborn": {
        "alias": "Высокородный",
        "positive": {"Fel", "Inf"},
        "negative": {"T"},
        "fate": 4,
        "blessing": 10,
    },
    "hive_world": {
        "alias": "Мир-улей",
        "positive": {"Ag", "Per"},
        "negative": {"WP"},
        "fate": 2,
        "blessing": 6,
    },
    "shrine_world": {
        "alias": "Мир-храм",
        "positive": {"Fel", "WP"},
        "negative": {"Per"},
        "fate": 3,
        "blessing": 6,
    },
    "voidborn": {
        "alias": "Пустоторождённый",
        "positive": {"Int", "WP"},
        "negative": {"S"},
        "fate": 3,
        "blessing": 5,
    },
}

CHARACTERISTICS = {"WS", "BS", "S", "T", "Ag", "Int", "Per", "WP", "Fel", "Inf"}


@dataclass
class _Roll:
    total: int


class _FakeRoller:
    def __init__(self, blessing: int):
        self.blessing = blessing
        self.calls: list[str] = []

    def roll_expression(self, expression: str) -> _Roll:
        self.calls.append(expression)
        if expression == "2d10+20":
            return _Roll(31)
        if expression == "3d10kh2+20":
            return _Roll(38)
        if expression == "3d10kl2+20":
            return _Roll(24)
        if expression == "1d10":
            return _Roll(self.blessing)
        raise AssertionError(f"unexpected roll expression: {expression}")


def test_dh2_home_world_profiles_match_core_book_table():
    pack = load_rulepack("dh2")
    profiles = pack.creation_constraints["profiles"]

    assert set(profiles) == set(HOME_WORLDS)
    for profile_id, expected in HOME_WORLDS.items():
        resolved = resolve_creation_profile(pack, expected["alias"])
        assert resolved is not None and resolved[0] == profile_id

        profile = profiles[profile_id]
        overrides = profile["attribute_rolls"]
        assert {key for key, expr in overrides.items() if expr == "3d10kh2+20"} == expected["positive"]
        assert {key for key, expr in overrides.items() if expr == "3d10kl2+20"} == expected["negative"]
        assert profile["attributes"]["FateThreshold"] == expected["fate"]
        assert profile["bonus_rolls"][0]["when"] == f"roll >= {expected['blessing']}"


def test_dh2_base_characteristic_generation_declares_two_d10_plus_twenty():
    pack = load_rulepack("dh2")
    rules = pack.creation_constraints["attributes"]

    assert set(rules) == CHARACTERISTICS
    assert all(rule["roll"] == "2d10+20" for rule in rules.values())


def test_profile_generation_applies_home_world_rolls_and_emperors_blessing():
    pack = load_rulepack("dh2")
    roller = _FakeRoller(blessing=3)

    result = generate_profiled_character(pack, "Дикий мир", "Acolyte", roller=roller)
    character = result.character

    assert result.profile_id == "feral_world"
    assert character.home_world == "Дикий мир"
    assert character.attributes["S"] == 38
    assert character.attributes["T"] == 38
    assert character.attributes["Inf"] == 24
    for key in CHARACTERISTICS - {"S", "T", "Inf"}:
        assert character.attributes[key] == 31

    # Feral World starts at 2 Fate; Emperor's Blessing succeeds on 3+.
    assert result.bonus_rolls["emperors_blessing"] == 3
    assert sheet_value(character, pack, "FateThreshold") == 3
    assert sheet_value(character, pack, "Fate") == 3

    meters = {entry["id"]: entry for entry in wire_resources(character, pack, "ru")}
    assert meters["fate"] == {"id": "fate", "label": "Судьба", "value": 3, "max": 3}


def test_profile_generation_leaves_fate_at_base_when_blessing_fails():
    pack = load_rulepack("dh2")
    roller = _FakeRoller(blessing=7)

    result = generate_profiled_character(pack, "Мир-кузница", "Acolyte", roller=roller)

    # Forge World requires 8+ for Emperor's Blessing.
    assert result.bonus_rolls["emperors_blessing"] == 7
    assert sheet_value(result.character, pack, "FateThreshold") == 3
    assert sheet_value(result.character, pack, "Fate") == 3


def test_every_dh2_home_world_profile_generates_with_real_dice_engine():
    pack = load_rulepack("dh2")

    for index, profile_id in enumerate(HOME_WORLDS):
        seed_dice(20260904 + index)
        result = generate_profiled_character(pack, profile_id, f"Acolyte-{index}")
        character = result.character

        assert set(result.attribute_rolls) == CHARACTERISTICS
        assert all(22 <= value <= 40 for value in result.attribute_rolls.values())
        base_fate = HOME_WORLDS[profile_id]["fate"]
        assert sheet_value(character, pack, "FateThreshold") in {base_fate, base_fate + 1}
        assert sheet_value(character, pack, "Fate") == sheet_value(character, pack, "FateThreshold")


def test_unknown_creation_profile_fails_loudly():
    pack = load_rulepack("dh2")

    with pytest.raises(CreationProfileError, match="unknown creation profile"):
        generate_profiled_character(pack, "агромир, который мы только что придумали", "Acolyte")
