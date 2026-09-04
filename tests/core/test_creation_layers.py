import pytest

from core.character_manager import CharacterSheet
from core.creation_layers import (
    CreationLayerError,
    apply_creation_layer,
    load_creation_layers,
    resolve_creation_layer_option,
)
from core.rulepacks import load_rulepack


BACKGROUND_ALIASES = {
    "Адептус Администратум": "adeptus_administratum",
    "Адептус Арбитрес": "adeptus_arbites",
    "Адептус Астра Телепатика": "adeptus_astra_telepathica",
    "Адептус Механикус": "adeptus_mechanicus",
    "Адептус Министорум": "adeptus_ministorum",
    "Астра Милитарум": "astra_militarum",
    "Изгой": "outcast",
}


def test_dh2_creation_sidecar_declares_all_seven_core_backgrounds():
    pack = load_rulepack("dh2")
    layers = load_creation_layers(pack)

    assert set(layers) == {"background"}
    assert set(layers["background"]["options"]) == set(BACKGROUND_ALIASES.values())
    for surface, canonical in BACKGROUND_ALIASES.items():
        resolved = resolve_creation_layer_option(pack, "background", surface)
        assert resolved is not None
        assert resolved[0] == canonical


def test_layer_application_refuses_to_guess_missing_player_choices():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(CreationLayerError, match="requires choices"):
        apply_creation_layer(pack, sheet, "background", "Адептус Администратум")

    assert sheet.background_choice == ""
    assert sheet.skills["Logic"] == 0
    assert sheet.equipment == []


def test_administratum_background_applies_fixed_and_selected_effects():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    result = apply_creation_layer(
        pack,
        sheet,
        "background",
        "Адептус Администратум",
        selections={
            "trained_skill": "Коммерция",
            "scholastic_lore": "Бюрократия",
            "weapon_training": "Лазерное",
            "starting_weapon": "Лазпистолет",
            "aptitude": "Познание",
        },
    )

    assert result.option_id == "adeptus_administratum"
    assert sheet.background_choice == "Адептус Администратум"
    assert sheet.skills["Logic"] == 1
    assert sheet.skills["Commerce"] == 1
    assert sheet.skills["CommonLore::адептус администратум"] == 1
    assert sheet.skills["Linguistics::высокий готик"] == 1
    assert sheet.skills["ScholasticLore::бюрократия"] == 1
    assert sheet.aptitudes == ["Познание"]
    assert sheet.talents == ["Выучка с Оружием (Лазерное)"]
    assert sheet.background_abilities == ["Мастер Бумажной Работы"]
    assert "лазпистолет" in sheet.equipment
    assert "медпакет" in sheet.equipment


def test_mechanicus_can_choose_a_free_operate_specialization_without_core_knowing_dh2():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(
        pack,
        sheet,
        "background",
        "Адептус Механикус",
        selections={
            "trained_skill": {"option": "Управление", "specialization": "Наземная"},
            "starting_weapon": "Автоган",
            "augment": "оптический механодендрит",
            "aptitude": "Техно",
        },
    )

    assert sheet.skills["Operate::наземная"] == 1
    assert sheet.skills["TechUse"] == 1
    assert sheet.traits == ["Имплантаты Механикус"]
    assert "Использование Механодендритов (Вспомогательный)" in sheet.talents
    assert sheet.aptitudes == ["Техно"]
    assert "оптический механодендрит" in sheet.equipment


def test_astra_militarum_background_uses_separate_navigation_and_operate_specializations():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(
        pack,
        sheet,
        "background",
        "Астра Милитарум",
        selections={
            "trained_skill": "Управление (Наземная)",
            "weapon_package": "Лазган",
            "aptitude": "Полевое",
        },
    )

    assert sheet.skills["Navigation::наземная"] == 1
    assert sheet.skills["Operate::наземная"] == 1
    assert sheet.skills["Athletics"] == 1
    assert sheet.aptitudes == ["Полевое"]
    assert "лазган" in sheet.equipment


def test_layer_rejects_unknown_or_extra_choice_instead_of_silently_ignoring_it():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(CreationLayerError, match="unknown choices"):
        apply_creation_layer(
            pack,
            sheet,
            "background",
            "Астра Милитарум",
            selections={
                "trained_skill": "Медика",
                "weapon_package": "Лазган",
                "aptitude": "Полевое",
                "surprise_me": "no",
            },
        )
