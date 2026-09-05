from dataclasses import dataclass

import pytest

from core.character_manager import CharacterSheet
from core.creation_finalization import (
    CreationFinalizationError,
    creation_finalization_status,
    load_creation_finalization_spec,
    resolve_creation_finalization,
    roll_creation_finalization,
)
from core.creation_flow import load_creation_flow_spec
from core.rulepacks import load_rulepack
from core.sheets import set_sheet_value, sheet_value


@dataclass
class _Roll:
    total: int


class _FixedRoller:
    def __init__(self, value: int):
        self.value = value
        self.calls: list[str] = []

    def roll_expression(self, expression: str) -> _Roll:
        self.calls.append(expression)
        return _Roll(self.value)


def _completed_creation_sheet() -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    flow = load_creation_flow_spec(pack)
    assert flow is not None
    sheet = CharacterSheet("Acolyte", "dh2")
    for canonical in ("WS", "BS", "S", "T", "Ag", "Int", "Per", "WP", "Fel", "Inf"):
        set_sheet_value(sheet, pack, canonical, 30)
    set_sheet_value(sheet, pack, "FateThreshold", 2)
    set_sheet_value(sheet, pack, "Fate", 2)
    sheet.secondary_attributes["__creation_flow__"] = {
        "version": 1,
        "profile_id": "feral_world",
        "profile_reroll": {"skipped": True},
        "stage_index": len(flow.stages),
        "completed": [stage.id for stage in flow.stages],
        "layers": {},
    }
    return pack, sheet


def test_dh2_finalization_table_covers_the_mandatory_divination_roll():
    spec = load_creation_finalization_spec(load_rulepack("dh2"))

    assert spec is not None
    assert spec.roll == "1d100"
    covered = {
        value
        for row in spec.rows
        for value in range(row.minimum, row.maximum + 1)
    }
    assert covered == set(range(1, 101))
    assert len(spec.rows) == 20


def test_choice_free_divination_applies_immediately_and_finalizes():
    pack, sheet = _completed_creation_sheet()
    roller = _FixedRoller(100)

    result = roll_creation_finalization(pack, sheet, roller=roller)

    assert roller.calls == ["1d100"]
    assert result.status.roll == 100
    assert result.status.row_id == "ask_how_you_serve"
    assert result.status.complete is True
    assert sheet_value(sheet, pack, "FateThreshold") == 3
    assert sheet_value(sheet, pack, "Fate") == 3

    persisted = creation_finalization_status(pack, CharacterSheet.from_dict(sheet.to_dict()))
    assert persisted is not None
    assert persisted.complete is True
    assert persisted.roll == 100


def test_divination_choices_are_pending_until_player_resolves_every_group():
    pack, sheet = _completed_creation_sheet()
    before = sheet.to_dict()

    rolled = roll_creation_finalization(pack, sheet, roller=_FixedRoller(18))

    assert rolled.status.row_id == "learn_from_death"
    assert rolled.status.complete is False
    assert sheet_value(sheet, pack, "Ag") == 30
    assert sheet_value(sheet, pack, "BS") == 30
    assert sheet.to_dict() != before  # only the one-shot finalization state was recorded

    with pytest.raises(CreationFinalizationError, match="requires choices"):
        resolve_creation_finalization(pack, sheet, {"increase": "Ловкость"})
    assert sheet_value(sheet, pack, "Ag") == 30
    assert sheet_value(sheet, pack, "BS") == 30

    resolved = resolve_creation_finalization(
        pack,
        sheet,
        {"increase": "Ловкость", "decrease": "Навык Стрельбы"},
    )
    assert resolved.status.complete is True
    assert sheet_value(sheet, pack, "Ag") == 33
    assert sheet_value(sheet, pack, "BS") == 27


def test_existing_talent_uses_the_declared_fallback_instead_of_duplicating_it():
    pack, sheet = _completed_creation_sheet()
    sheet.talents = ["Искушённый"]

    result = roll_creation_finalization(pack, sheet, roller=_FixedRoller(6))

    assert result.status.complete is True
    assert sheet.talents == ["Искушённый"]
    assert sheet_value(sheet, pack, "WP") == 32


def test_roll_one_is_preserved_and_blocked_without_silent_reroll_or_completion():
    pack, sheet = _completed_creation_sheet()

    result = roll_creation_finalization(pack, sheet, roller=_FixedRoller(1))

    assert result.status.roll == 1
    assert result.status.row_id == "mutation_without_corruption"
    assert result.status.complete is False
    assert result.status.blocked_reference == "table_8_15_rudiments"

    with pytest.raises(CreationFinalizationError, match="already been rolled"):
        roll_creation_finalization(pack, sheet, roller=_FixedRoller(100))
    status = creation_finalization_status(pack, sheet)
    assert status is not None and status.roll == 1 and status.complete is False


def test_finalization_refuses_to_run_before_staged_creation_is_complete():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Too Early", "dh2")

    with pytest.raises(CreationFinalizationError, match="staged character creation must be complete"):
        roll_creation_finalization(pack, sheet, roller=_FixedRoller(100))
    assert creation_finalization_status(pack, sheet) is None


def test_reading_finalization_status_does_not_initialize_any_state():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Legacy", "dh2")

    assert creation_finalization_status(pack, sheet) is None
    assert "__creation_finalization__" not in sheet.secondary_attributes
