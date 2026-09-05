from dataclasses import dataclass

import pytest

from core.advancement_surface import initialized_advancement_budget
from core.character_manager import CharacterSheet
from core.creation_flow import (
    CreationFlowError,
    apply_creation_flow_layer,
    choose_creation_flow_starting_item,
    creation_flow_duplicate_requirements,
    creation_flow_status,
    finish_creation_advancement,
    load_creation_flow_spec,
    resolve_creation_flow_duplicates,
    start_creation_flow,
)
from core.creation_layers import CreationLayerError
from core.rulepacks import load_rulepack
from core.starting_equipment import starting_equipment_budget


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


def _administratum_choices() -> dict[str, object]:
    return {
        "trained_skill": "Коммерция",
        "scholastic_lore": "Бюрократия",
        "weapon_training": "Лазерное",
        "starting_weapon": "Лазпистолет",
        "aptitude": "Познание",
    }


def test_dh2_creation_flow_declares_explicit_generic_stage_order():
    spec = load_creation_flow_spec(load_rulepack("dh2"))

    assert spec is not None
    assert [(stage.id, stage.kind) for stage in spec.stages] == [
        ("characteristics", "profile"),
        ("home_world", "layer"),
        ("background", "layer"),
        ("role", "layer"),
        ("duplicate_aptitudes", "duplicates"),
        ("advancement", "advancement"),
        ("starting_equipment", "starting_equipment"),
    ]
    assert spec.stages[1].layer_id == "home_world"
    assert spec.stages[1].option_from_profile is True
    assert spec.stages[2].defer_duplicates is True
    assert spec.stages[3].defer_duplicates is True


def test_dh2_full_creation_flow_reaches_ready_character_without_system_specific_core_logic():
    pack = load_rulepack("dh2")
    roller = _FixedRoller(30)

    started = start_creation_flow(pack, "Дикий мир", "Acolyte", roller=roller)
    sheet = started.character

    assert started.profile_id == "feral_world"
    assert started.status.stage_id == "home_world"
    assert started.status.stage_kind == "layer"
    assert initialized_advancement_budget(pack, sheet) is None
    assert starting_equipment_budget(pack, sheet) is None

    home_world = apply_creation_flow_layer(pack, sheet, roller=roller)
    assert home_world.result.option_id == "feral_world"
    assert home_world.status.stage_id == "background"

    background = apply_creation_flow_layer(
        pack,
        sheet,
        "Адептус Администратум",
        selections=_administratum_choices(),
        roller=roller,
    )
    assert background.result.option_id == "adeptus_administratum"
    assert background.status.stage_id == "role"

    role = apply_creation_flow_layer(
        pack,
        sheet,
        "Хирургеон",
        selections={"role_talent": "Нокдаун"},
        roller=roller,
    )
    assert role.result.option_id == "chirurgeon"
    assert role.status.stage_id == "duplicate_aptitudes"
    assert role.status.stage_kind == "duplicates"
    assert initialized_advancement_budget(pack, sheet) is None

    requirements = creation_flow_duplicate_requirements(pack, sheet)
    assert len(requirements) == 1
    assert requirements[0].field == "Aptitudes"
    assert requirements[0].count == 2
    assert "Навык Стрельбы" in requirements[0].choices
    assert "Навык Рукопашной" in requirements[0].choices

    duplicate_result = resolve_creation_flow_duplicates(
        pack,
        sheet,
        {"Aptitudes": ["Навык Стрельбы", "Навык Рукопашной"]},
    )
    assert duplicate_result.replacements == {
        "Aptitudes": ("Навык Стрельбы", "Навык Рукопашной"),
    }
    assert duplicate_result.status.stage_id == "advancement"
    assert len(sheet.aptitudes) == len(set(sheet.aptitudes))

    xp = initialized_advancement_budget(pack, sheet)
    assert xp is not None
    assert xp.starting_xp == 1000
    assert xp.available_xp == 1000
    assert starting_equipment_budget(pack, sheet) is None

    equipment_stage = finish_creation_advancement(pack, sheet)
    assert equipment_stage.stage_id == "starting_equipment"
    equipment_budget = starting_equipment_budget(pack, sheet)
    assert equipment_budget is not None
    assert equipment_budget.total == 3
    assert equipment_budget.remaining == 3

    first = choose_creation_flow_starting_item(pack, sheet, "Лазган")
    assert first.status.complete is False
    assert first.grant.budget.remaining == 2
    assert "Лазган" in first.grant.equipment_added
    assert any("2 магазина" in item for item in first.grant.equipment_added)

    second = choose_creation_flow_starting_item(pack, sheet, "Нож")
    assert second.grant.budget.remaining == 1
    assert second.grant.equipment_added == ("Нож",)

    third = choose_creation_flow_starting_item(pack, sheet, "Респиратор")
    assert third.grant.budget.remaining == 0
    assert third.status.complete is True
    assert third.status.stage_id is None
    assert third.status.completed_stages == (
        "characteristics",
        "home_world",
        "background",
        "role",
        "duplicate_aptitudes",
        "advancement",
        "starting_equipment",
    )

    persisted = creation_flow_status(pack, CharacterSheet.from_dict(sheet.to_dict()))
    assert persisted is not None
    assert persisted.complete is True


def test_duplicate_resolution_rejects_missing_or_already_owned_choices_atomically():
    pack = load_rulepack("dh2")
    roller = _FixedRoller(30)
    sheet = start_creation_flow(pack, "Дикий мир", "Acolyte", roller=roller).character
    apply_creation_flow_layer(pack, sheet, roller=roller)
    apply_creation_flow_layer(
        pack,
        sheet,
        "Адептус Администратум",
        selections=_administratum_choices(),
        roller=roller,
    )
    apply_creation_flow_layer(
        pack,
        sheet,
        "Хирургеон",
        selections={"role_talent": "Нокдаун"},
        roller=roller,
    )
    before = sheet.to_dict()

    with pytest.raises(CreationLayerError, match="requires exactly 2 choices"):
        resolve_creation_flow_duplicates(pack, sheet, {"Aptitudes": ["Навык Стрельбы"]})
    assert sheet.to_dict() == before

    with pytest.raises(CreationLayerError, match="is already present"):
        resolve_creation_flow_duplicates(
            pack,
            sheet,
            {"Aptitudes": ["Выносливость", "Навык Рукопашной"]},
        )
    assert sheet.to_dict() == before
    assert initialized_advancement_budget(pack, sheet) is None


def test_profile_bound_layer_cannot_switch_to_a_different_option_and_rolls_back():
    pack = load_rulepack("dh2")
    started = start_creation_flow(pack, "Дикий мир", "Acolyte", roller=_FixedRoller(30))
    sheet = started.character
    before = sheet.to_dict()

    with pytest.raises(CreationFlowError, match="must reuse profile option"):
        apply_creation_flow_layer(pack, sheet, "Мир-улей", roller=_FixedRoller(30))

    assert sheet.to_dict() == before
    status = creation_flow_status(pack, sheet)
    assert status is not None
    assert status.stage_id == "home_world"


def test_reading_creation_flow_status_never_initializes_legacy_sheet_budgets():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Legacy", "dh2")

    assert creation_flow_status(pack, sheet) is None
    assert initialized_advancement_budget(pack, sheet) is None
    assert starting_equipment_budget(pack, sheet) is None
    assert "__creation_flow__" not in sheet.secondary_attributes
