from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from core.sheets import set_sheet_value, sheet_value
from core.talent_requirements import talent_requirements_met


def test_dh2_sheet_declares_numeric_psy_rating_and_structured_implants():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Acolyte", "dh2")

    assert sheet_value(sheet, pack, "PsyRating") == 0
    assert pack.resolve_skill("Пси-рейтинг") == "PsyRating"
    assert sheet.implants == []

    set_sheet_value(sheet, pack, "Пси-рейтинг", 2)
    assert sheet_value(sheet, pack, "PsyRating") == 2
    assert sheet.attributes["PSY_RATING"] == 2


def test_real_psy_rating_unlocks_psy_rating_prerequisite_without_legacy_elite_marker():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Psyker", "dh2")
    sheet.attributes["Per"] = 30
    sheet.attributes["PSY_RATING"] = 1
    sheet.skills["Psyniscience"] = 1

    assert sheet.elite_advances == []
    assert talent_requirements_met(pack, sheet, "Варп-чувство")


def test_legacy_psyker_elite_marker_remains_a_psy_rating_prerequisite_fallback():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Legacy Psyker", "dh2")
    sheet.attributes["Per"] = 30
    sheet.skills["Psyniscience"] = 1
    sheet.elite_advances.append("Псайкер")

    assert sheet_value(sheet, pack, "PsyRating") == 0
    assert talent_requirements_met(pack, sheet, "Варп-чувство")


def test_warp_lock_resolves_okp_strong_minded_name_to_unyielding_talent():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Psyker", "dh2")
    sheet.attributes["WP"] = 50
    sheet.attributes["PSY_RATING"] = 1
    sheet.talents.append("Непреклонный")

    assert talent_requirements_met(pack, sheet, "Варп-блок")


def test_structured_implant_field_unlocks_cybernetic_talent_requirement():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Tech-Priest", "dh2")
    sheet.traits.append("Имплантаты Механикус")
    sheet.implants.append("Конденсатор люминена")

    assert talent_requirements_met(pack, sheet, "Шок Люминена")


def test_legacy_equipment_storage_remains_an_implant_requirement_fallback():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Legacy Tech-Priest", "dh2")
    sheet.traits.append("Имплантаты Механикус")
    sheet.equipment.append("Конденсатор люминена")

    assert sheet.implants == []
    assert talent_requirements_met(pack, sheet, "Шок Люминена")
