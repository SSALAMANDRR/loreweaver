from types import SimpleNamespace

import pytest
import yaml

from core.creation_presentation import (
    CreationPresentationError,
    input_presentation,
    load_creation_presentation,
    presentation_label,
    stage_presentation,
)
from core.rulepacks import load_rulepack


def test_dh2_creation_presentation_localizes_stage_guidance_without_core_rule_knowledge():
    pack = load_rulepack("dh2")

    ru = stage_presentation(pack, "role", "ru")
    en = stage_presentation(pack, "role", "en")

    assert ru["title"] == "Роль"
    assert "не обязанность служить Инквизиции" in ru["description"]
    assert en["title"] == "Role"
    assert "does not require service to the Inquisition" in en["description"]


def test_dh2_creation_presentation_localizes_generic_ui_tokens():
    pack = load_rulepack("dh2")

    assert (
        presentation_label(pack, "choice_groups", "trained_skill", "ru", "trained_skill")
        == "Обученное умение"
    )
    assert (
        presentation_label(pack, "advancement_stages", "simple", "ru", "simple")
        == "Простое"
    )
    assert (
        presentation_label(pack, "advancement_categories", "talent", "ru", "talent")
        == "Талант"
    )
    assert (
        presentation_label(pack, "advancement_stages", "unknown", "ru", "fallback")
        == "fallback"
    )


def test_dh2_input_guidance_is_localized_and_falls_back_to_english():
    pack = load_rulepack("dh2")
    ru = input_presentation(pack, "choice_group_inputs", "scholastic_lore", "ru-RU")
    assert ru == {
        "label": "Специализация: Учёные знания",
        "placeholder": "Например, Бюрократия",
        "description": "Введите название области знаний. Укажите только специализацию, без названия навыка.",
    }
    en = input_presentation(pack, "choice_group_inputs", "scholastic_lore", "en")
    assert en["label"] == "Specialization: Scholastic Lore"
    assert input_presentation(pack, "choice_group_inputs", "scholastic_lore", "zh") == en
    assert input_presentation(pack, "choice_group_inputs", "missing", "ru") == {}


@pytest.mark.parametrize("section", ["choice_group_inputs", "choice_option_inputs"])
def test_input_sidecar_loading_and_per_field_locale_fallback(tmp_path, section):
    pack = SimpleNamespace(system="synthetic")
    path = tmp_path / pack.system / "creation_presentation.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({section: {"craft": {
        "en": {"label": "Craft", "placeholder": "A subject", "description": "Name a subject"},
        "ru": {"label": "Ремесло", "description": " "},
        "ru-ru": {"placeholder": "Область"},
    }}}), encoding="utf-8")
    data = load_creation_presentation(pack, data_root=tmp_path)
    assert input_presentation(pack, section, "craft", "RU_RU", presentation=data) == {
        "label": "Ремесло", "placeholder": "Область", "description": "Name a subject",
    }


@pytest.mark.parametrize("entry", [
    [], {"": {}}, {"en": []}, {"en": {"unknown": "text"}},
    {"en": {"label": 12}}, {"en": {"description": None}},
])
@pytest.mark.parametrize("section", ["choice_group_inputs", "choice_option_inputs"])
def test_input_sidecar_rejects_invalid_metadata(tmp_path, section, entry):
    pack = SimpleNamespace(system="synthetic")
    path = tmp_path / pack.system / "creation_presentation.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({section: {"craft": entry}}), encoding="utf-8")
    with pytest.raises(CreationPresentationError):
        load_creation_presentation(pack, data_root=tmp_path)


def test_legacy_presentation_without_inputs_and_missing_sidecar(tmp_path):
    pack = SimpleNamespace(system="synthetic")
    assert load_creation_presentation(pack, data_root=tmp_path) == {}
    path = tmp_path / pack.system / "creation_presentation.yaml"
    path.parent.mkdir()
    path.write_text("version: 1\nchoice_groups: {}\n", encoding="utf-8")
    data = load_creation_presentation(pack, data_root=tmp_path)
    assert input_presentation(pack, "choice_group_inputs", "craft", "en", presentation=data) == {}
    with pytest.raises(CreationPresentationError):
        input_presentation(pack, "terms", "craft", "en", presentation=data)


@pytest.mark.parametrize("value", [[], None, "text", 0])
def test_input_sidecar_rejects_non_mapping_section(tmp_path, value):
    pack = SimpleNamespace(system="synthetic")
    path = tmp_path / pack.system / "creation_presentation.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({"choice_group_inputs": value}), encoding="utf-8")
    with pytest.raises(CreationPresentationError):
        load_creation_presentation(pack, data_root=tmp_path)


@pytest.mark.parametrize("specialization", [
    {"skill_family": "StarCraft"},
    {"field_template": {"field": "Traits", "template": "Craft ({specialization})"}},
])
def test_input_projection_is_generic_and_only_describes_text_fields(specialization):
    from core.creation_surface import _choice_wire

    pack = SimpleNamespace()
    guidance = {"label": "Subject", "placeholder": "Comets", "description": "Name a subject"}
    presentation = {
        "choice_group_inputs": {"craft": {"en": guidance}},
        "choice_option_inputs": {"special": {"en": guidance}, "plain": {"en": guidance}},
    }
    raw = {"options": {"special": specialization, "plain": {}}}
    row = _choice_wire(pack, "craft", raw, "en", presentation)
    assert row["free"] is False
    assert "input" not in row
    special, plain = row["options"]
    assert special.pop("input") == guidance
    assert "input" not in plain
    assert row == _choice_wire(pack, "craft", raw, "en", {})
