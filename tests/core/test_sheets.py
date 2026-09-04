"""Tests for core.sheets: the generic pack-declared sheet substrate (M16 stage B).

Everything here goes through the PUBLIC spec-driven API against the bundled
packs — no per-system code paths exist to test anymore; the packs' YAML is the
system-specific half.
"""

import pytest

from core.character_manager import CharacterSheet, get_hit_points
from core.rulepacks import load_rulepack, parse_rulepack_text
from core.sheets import (
    SheetSpecError,
    UntrainedSkillError,
    canonical_values,
    check_value,
    has_check_value,
    parse_sheet_section,
    refresh_sheet,
    resolve_skill_family,
    set_sheet_value,
    sheet_value,
    wire_resources,
)


def test_sheet_value_reads_attributes_skills_and_derived():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")
    sheet.attributes["STR"] = 65

    assert sheet_value(sheet, pack, "力量") == 65
    assert sheet_value(sheet, pack, "侦查") == 25
    sheet.attributes["DEX"] = 80
    sheet.skills["闪避"] = 1
    refresh_sheet(sheet, pack, preserve_trained=False)
    assert sheet_value(sheet, pack, "闪避") == 40


def test_set_sheet_value_routes_to_declared_storage_slots():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")

    set_sheet_value(sheet, pack, "力量", 70)
    assert sheet.attributes["STR"] == 70

    set_sheet_value(sheet, pack, "信用评级", 45)
    assert sheet.skills["信用"] == 45

    set_sheet_value(sheet, pack, "自定义技能", 33)
    assert sheet.skills["自定义技能"] == 33


def test_set_attribute_refreshes_dependent_derived_slots():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")

    set_sheet_value(sheet, pack, "敏捷", 90)
    assert sheet_value(sheet, pack, "闪避") == 45

    set_sheet_value(sheet, pack, "教育", 80)
    assert sheet_value(sheet, pack, "母语") == 80


def test_trained_derived_skill_survives_refresh():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")
    sheet.skills["闪避"] = 60

    refresh_sheet(sheet, pack)

    assert sheet.skills["闪避"] == 60


def test_refresh_clamps_current_vitals_and_initializes_missing_ones():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")
    sheet.attributes["HP"] = 4

    refresh_sheet(sheet, pack)
    assert sheet.attributes["HP"] == 4

    sheet.attributes["CON"] = 30
    sheet.attributes["SIZ"] = 30
    refresh_sheet(sheet, pack)
    assert sheet.attributes["HPMAX"] == 6
    assert sheet.attributes["HP"] == 4

    refresh_sheet(sheet, pack, initialize_vitals=True)
    assert sheet.attributes["HP"] == 6


def test_dnd_sheet_secondary_and_field_bridges():
    pack = load_rulepack("dnd5e")
    sheet = CharacterSheet("Kael", "DnD5e")
    sheet.attributes["DEX"] = 16
    refresh_sheet(sheet, pack, preserve_trained=False)

    assert sheet_value(sheet, pack, "护甲等级") == 13
    assert sheet_value(sheet, pack, "等级") == 1
    set_sheet_value(sheet, pack, "等级", 5)
    assert sheet.level == 5
    refresh_sheet(sheet, pack)
    assert sheet_value(sheet, pack, "熟练加值") == 3

    hp, hp_max = get_hit_points(sheet)
    assert sheet_value(sheet, pack, "hp") == hp
    assert sheet_value(sheet, pack, "hpmax") == hp_max


def test_check_value_bridges_ability_checks_to_modifiers():
    pack = load_rulepack("dnd5e")
    sheet = CharacterSheet("Kael", "DnD5e")
    sheet.attributes["STR"] = 16
    refresh_sheet(sheet, pack, preserve_trained=False)

    assert check_value(sheet, pack, "力量") == 3
    assert check_value(sheet, pack, "运动") == 3

    coc = load_rulepack("coc7")
    investigator = CharacterSheet("调查员", "CoC")
    investigator.attributes["STR"] = 65
    assert check_value(investigator, coc, "力量") == 65


def test_has_check_value_accepts_known_names_and_rejects_garbage():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")

    assert has_check_value(sheet, pack, "侦查")
    assert has_check_value(sheet, pack, "力量")
    assert has_check_value(sheet, pack, "STR")
    sheet.skills["祖传菜刀"] = 40
    assert has_check_value(sheet, pack, "祖传菜刀")
    assert not has_check_value(sheet, pack, "不存在的技能")


def test_wire_resources_lists_declared_meters():
    coc = load_rulepack("coc7")
    investigator = CharacterSheet("调查员", "CoC")
    meters = {entry["id"]: entry for entry in wire_resources(investigator, coc)}
    assert set(meters) == {"hp", "san", "mp"}
    assert meters["hp"] == {"id": "hp", "label": "HP", "value": 10, "max": 10}

    dnd = load_rulepack("dnd5e")
    fighter = CharacterSheet("Kael", "DnD5e")
    meters = {entry["id"]: entry for entry in wire_resources(fighter, dnd)}
    assert set(meters) == {"hp"}
    assert meters["hp"]["value"] == 8 and meters["hp"]["max"] == 8


def test_canonical_values_translate_storage_keys():
    pack = load_rulepack("coc7")
    sheet = CharacterSheet("调查员", "CoC")
    sheet.attributes["STR"] = 65
    sheet.skills["信用"] = 30

    values = canonical_values(sheet, pack)
    assert values["力量"] == 65
    assert values["信用评级"] == 30
    assert values["职业"] == ""


_LOCALIZED_SHEET = {
    "label": "潮占者",
    "attributes": {"CHAO": 3, "CHAOMAX": 9},
    "resources": [
        {"id": "chao", "label": {"en": "Tide", "zh": "潮位"}, "value": "CHAO", "max": "CHAOMAX"},
        {"id": "plain", "label": "Ledger", "value": "CHAO", "max": "CHAOMAX"},
        {"id": "zh_only", "label": {"zh": "灯签"}, "value": "CHAO", "max": "CHAOMAX"},
    ],
}


def test_resource_labels_accept_a_string_or_a_locale_map():
    spec = parse_sheet_section("chaozhan", _LOCALIZED_SHEET)
    tide, plain, zh_only = spec.resources

    assert (tide.label_for("zh"), tide.label_for("en"), tide.label_for(None)) == ("潮位", "Tide", "Tide")
    assert plain.labels == {"en": "Ledger"} and plain.label_for("zh") == "Ledger"
    assert zh_only.label_for("en") == "灯签"
    assert tide.label_for("zh-Hans") == "潮位"


def test_resource_label_errors_are_author_actionable():
    for bad in ({}, "", 7, {"zh": "  "}, [".."]):
        entry = {"id": "x", "label": bad, "value": "CHAO", "max": "CHAOMAX"}
        with pytest.raises(SheetSpecError, match="label"):
            parse_sheet_section("chaozhan", {**_LOCALIZED_SHEET, "resources": [entry]})


def test_wire_resources_resolves_labels_to_the_viewer_locale():
    class _Pack:
        sheet_spec = parse_sheet_section("chaozhan", _LOCALIZED_SHEET)

    class _Sheet:
        attributes = {"CHAO": 4, "CHAOMAX": 9}

    zh = {entry["id"]: entry["label"] for entry in wire_resources(_Sheet(), _Pack(), "zh")}
    en = {entry["id"]: entry["label"] for entry in wire_resources(_Sheet(), _Pack(), "en")}
    assert zh["chao"] == "潮位" and en["chao"] == "Tide"
    assert zh["plain"] == en["plain"] == "Ledger"


_SKILL_FAMILY_PACK = """
defaults:
  INT: 40
alias:
  INT: [Intelligence]
sheet:
  label: Family test
  attr_keys: {INT: INT}
  attributes: {INT: 40}
  skill_families:
    Lore:
      base: INT
      aliases: [Knowledge]
      ranks: {1: 0, 2: 10, 3: 20}
      untrained: forbidden
    Craft:
      base: INT
      aliases: [Crafting]
      ranks: {1: 0, 2: 5}
      untrained: -20
"""


def test_skill_family_specializations_are_normalized_and_stored_independently():
    pack = parse_rulepack_text("family-test", _SKILL_FAMILY_PACK)
    sheet = CharacterSheet("Scholar", "family-test")
    sheet.attributes["INT"] = 40

    warp = resolve_skill_family(pack, "Knowledge (Warp)")
    archive = resolve_skill_family(pack, "Lore::ARCHIVE")
    assert warp is not None and warp[0] == "Lore::warp"
    assert archive is not None and archive[0] == "Lore::archive"
    assert resolve_skill_family(pack, "Knowledge") is None

    set_sheet_value(sheet, pack, "Knowledge (Warp)", 2)
    set_sheet_value(sheet, pack, "Knowledge (Archive)", 3)

    assert sheet.skills == {"Lore::warp": 2, "Lore::archive": 3}
    assert check_value(sheet, pack, "Knowledge (Warp)") == 50
    assert check_value(sheet, pack, "Lore::archive") == 60


def test_skill_family_untrained_policy_is_data_driven():
    pack = parse_rulepack_text("family-test", _SKILL_FAMILY_PACK)
    sheet = CharacterSheet("Scholar", "family-test")
    sheet.attributes["INT"] = 40

    assert not has_check_value(sheet, pack, "Knowledge (Warp)")
    with pytest.raises(UntrainedSkillError):
        check_value(sheet, pack, "Knowledge (Warp)")

    assert has_check_value(sheet, pack, "Crafting (Wood)")
    assert check_value(sheet, pack, "Crafting (Wood)") == 20


def test_skill_family_schema_rejects_ambiguous_aliases_and_bad_ranks():
    with pytest.raises(SheetSpecError, match="claimed by both"):
        parse_sheet_section(
            "bad",
            {
                "label": "bad",
                "skill_families": {
                    "One": {"base": "INT", "aliases": ["Shared"], "ranks": {1: 0}},
                    "Two": {"base": "INT", "aliases": ["shared"], "ranks": {1: 0}},
                },
            },
        )

    with pytest.raises(SheetSpecError, match="ranks start at 1"):
        parse_sheet_section(
            "bad",
            {
                "label": "bad",
                "skill_families": {
                    "One": {"base": "INT", "ranks": {0: -20, 1: 0}},
                },
            },
        )
