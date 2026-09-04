from dataclasses import dataclass

import pytest

from core.character_manager import CharacterSheet
from core.creation_layers import (
    CreationLayerError,
    apply_creation_layer,
    load_creation_layers,
    resolve_creation_layer_option,
)
from core.rulepacks import load_rulepack
from core.sheets import sheet_value


HOME_WORLD_ALIASES = {
    "Дикий мир": "feral_world",
    "Мир-кузница": "forge_world",
    "Высокородный": "highborn",
    "Мир-улей": "hive_world",
    "Мир-храм": "shrine_world",
    "Пустоторождённый": "voidborn",
}

BACKGROUND_ALIASES = {
    "Адептус Администратум": "adeptus_administratum",
    "Адептус Арбитрес": "adeptus_arbites",
    "Адептус Астра Телепатика": "adeptus_astra_telepathica",
    "Адептус Механикус": "adeptus_mechanicus",
    "Адептус Министорум": "adeptus_ministorum",
    "Астра Милитарум": "astra_militarum",
    "Изгой": "outcast",
}

ROLE_ALIASES = {
    "Ассасин": "assassin",
    "Хирургеон": "chirurgeon",
    "Десперадо": "desperado",
    "Иерофант": "hierophant",
    "Мистик": "mystic",
    "Мудрец": "sage",
    "Искатель": "seeker",
    "Воитель": "warrior",
}


@dataclass
class _Roll:
    total: int


class _FixedLayerRoller:
    def __init__(self, value: int):
        self.value = value
        self.calls: list[str] = []

    def roll_expression(self, expression: str) -> _Roll:
        self.calls.append(expression)
        return _Roll(self.value)


def test_dh2_creation_sidecars_declare_home_worlds_backgrounds_and_roles():
    pack = load_rulepack("dh2")
    layers = load_creation_layers(pack)

    assert set(layers) == {"home_world", "background", "role"}
    assert set(layers["home_world"]["options"]) == set(HOME_WORLD_ALIASES.values())
    assert set(layers["background"]["options"]) == set(BACKGROUND_ALIASES.values())
    assert set(layers["role"]["options"]) == set(ROLE_ALIASES.values())

    for surface, canonical in HOME_WORLD_ALIASES.items():
        resolved = resolve_creation_layer_option(pack, "home_world", surface)
        assert resolved is not None
        assert resolved[0] == canonical

    for surface, canonical in BACKGROUND_ALIASES.items():
        resolved = resolve_creation_layer_option(pack, "background", surface)
        assert resolved is not None
        assert resolved[0] == canonical

    for surface, canonical in ROLE_ALIASES.items():
        resolved = resolve_creation_layer_option(pack, "role", surface)
        assert resolved is not None
        assert resolved[0] == canonical


def test_feral_home_world_applies_wounds_aptitude_and_ability_without_core_knowing_dh2():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")
    roller = _FixedLayerRoller(12)

    result = apply_creation_layer(pack, sheet, "home_world", "Дикий мир", roller=roller)

    assert result.option_id == "feral_world"
    assert roller.calls == ["1d5+9"]
    assert sheet_value(sheet, pack, "Wounds") == 12
    assert sheet.aptitudes == ["Выносливость"]
    assert sheet.background_abilities == ["Старые Пути"]


def test_forge_home_world_refuses_to_guess_omnissiah_talent_then_applies_player_choice():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(CreationLayerError, match="requires choices"):
        apply_creation_layer(pack, sheet, "home_world", "Мир-кузница", roller=_FixedLayerRoller(11))

    assert sheet.talents == []
    assert sheet.aptitudes == []

    apply_creation_layer(
        pack,
        sheet,
        "home_world",
        "Мир-кузница",
        selections={"home_world_talent": "Длань Омниссии"},
        roller=_FixedLayerRoller(11),
    )

    assert sheet_value(sheet, pack, "Wounds") == 11
    assert sheet.aptitudes == ["Интеллект"]
    assert sheet.background_abilities == ["Избранный Омниссии"]
    assert sheet.talents == ["Длань Омниссии"]


def test_voidborn_home_world_grants_fixed_unyielding_talent_and_seven_plus_d5_wounds():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(pack, sheet, "home_world", "Пустоторождённый", roller=_FixedLayerRoller(9))

    assert sheet_value(sheet, pack, "Wounds") == 9
    assert sheet.aptitudes == ["Интеллект"]
    assert sheet.talents == ["Непреклонный"]
    assert sheet.background_abilities == ["Дитя Темноты"]


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


def test_assassin_role_applies_fixed_aptitudes_and_keeps_both_player_choices_explicit():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    result = apply_creation_layer(
        pack,
        sheet,
        "role",
        "Ассасин",
        selections={
            "combat_aptitude": "Навык Стрельбы",
            "role_talent": "Вскочить",
        },
    )

    assert result.option_id == "assassin"
    assert sheet.role_choice == "Ассасин"
    assert sheet.aptitudes == ["Ловкость", "Полевое", "Изящество", "Восприятие", "Навык Стрельбы"]
    assert sheet.talents == ["Вскочить"]
    assert sheet.role_abilities == ["Уверенное Убийство"]


def test_specialized_list_field_choice_records_the_players_actual_talent_specialization():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(
        pack,
        sheet,
        "role",
        "Хирургеон",
        selections={
            "role_talent": {"option": "Сопротивление", "specialization": "Яды"},
        },
    )

    assert sheet.role_choice == "Хирургеон"
    assert sheet.talents == ["Сопротивление (Яды)"]
    assert sheet.role_abilities == ["Преданный Целитель"]
    assert sheet.aptitudes == ["Полевое", "Интеллект", "Познание", "Сила", "Выносливость"]


def test_hierophant_hatred_uses_the_same_generic_specialized_field_primitive():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(
        pack,
        sheet,
        "role",
        "Иерофант",
        selections={
            "role_talent": {"option": "Ненависть", "specialization": "Еретики"},
        },
    )

    assert "Ненависть (Еретики)" in sheet.talents
    assert "Власть над Массами" in sheet.role_abilities


def test_mystic_role_records_psyker_elite_advance_without_pretending_to_execute_it_yet():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    apply_creation_layer(
        pack,
        sheet,
        "role",
        "Мистик",
        selections={"role_talent": "Варп-чувство"},
    )

    assert sheet.role_choice == "Мистик"
    assert sheet.elite_advances == ["Псайкер"]
    assert sheet.talents == ["Варп-чувство"]
    assert sheet.role_abilities == ["Смотрящий в Варп"]


def test_all_eight_roles_have_five_role_aptitudes_and_one_role_ability_after_choices():
    pack = load_rulepack("dh2")
    selections = {
        "Ассасин": {"combat_aptitude": "Навык Рукопашной", "role_talent": "Искушённый"},
        "Хирургеон": {"role_talent": "Нокдаун"},
        "Десперадо": {"role_talent": "Выхватить Оружие"},
        "Иерофант": {"role_talent": "Плечом к Плечу"},
        "Мистик": {"role_talent": "Сопротивление (Психические Силы)"},
        "Мудрец": {"role_talent": "Амбидекстрия"},
        "Искатель": {"role_talent": "Разоружение"},
        "Воитель": {"role_talent": "Железная Челюсть"},
    }

    for role, role_selections in selections.items():
        sheet = CharacterSheet(role, "dh2")
        apply_creation_layer(pack, sheet, "role", role, selections=role_selections)
        assert sheet.role_choice == role
        assert len(sheet.aptitudes) == 5
        assert len(sheet.role_abilities) == 1
        assert len(sheet.talents) == 1


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
