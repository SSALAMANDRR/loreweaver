from core.creation_presentation import presentation_label, stage_presentation
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

    assert presentation_label(pack, "choice_groups", "trained_skill", "ru", "trained_skill") == "Обученное умение"
    assert presentation_label(pack, "advancement_stages", "simple", "ru", "simple") == "Простое"
    assert presentation_label(pack, "advancement_categories", "talent", "ru", "talent") == "Талант"
    assert presentation_label(pack, "advancement_stages", "unknown", "ru", "fallback") == "fallback"
