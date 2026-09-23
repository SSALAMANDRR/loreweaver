"""Opponent profiles → ordinary sheets. Expected values are transcribed from the
stat blocks cited in rulepacks/data/dh2/npc_profiles.yaml (Глава XII с. 485/486,
Глава XIII с. 543)."""

import pytest

import core.npc_profiles as npc_profiles
from core.npc_profiles import NpcProfileError, find_npc_profile, load_npc_profiles, materialize_npc_sheet
from core.rulepacks import load_rulepack
from core.sheets import sheet_value


@pytest.fixture(scope="module")
def pack():
    return load_rulepack("dh2")


def _sheet(pack, profile_id, name="N"):
    return materialize_npc_sheet(load_npc_profiles(pack)[profile_id], name, pack)


def test_the_three_verified_profiles_load_with_their_sources(pack):
    profiles = load_npc_profiles(pack)
    assert set(profiles) == {"hive_scum", "desoleum_oathsworn_trooper", "hired_gun"}
    assert all(profile.npc_type == "troop" and profile.source_reference for profile in profiles.values())
    assert find_npc_profile(profiles, "пехотинец").id == "desoleum_oathsworn_trooper"


@pytest.mark.parametrize(
    ("profile_id", "characteristics", "wounds", "tb", "total_body_protection"),
    [
        ("hive_scum", {"WS": 30, "BS": 25, "S": 26, "T": 24, "Ag": 32, "Int": 26, "Per": 37, "WP": 28, "Fel": 30}, 9, 2, 2),
        ("desoleum_oathsworn_trooper", {"WS": 36, "BS": 43, "S": 38, "T": 42, "Ag": 35, "Int": 28, "Per": 44, "WP": 32, "Fel": 36}, 11, 4, 8),
        ("hired_gun", {"WS": 35, "BS": 37, "S": 35, "T": 41, "Ag": 32, "Int": 28, "Per": 35, "WP": 32, "Fel": 25}, 10, 4, 8),
    ],
)
def test_materialized_sheet_matches_the_stat_block(pack, profile_id, characteristics, wounds, tb, total_body_protection):
    from core.item_model import load_item_catalog

    sheet = _sheet(pack, profile_id)
    assert {key: sheet.attributes[key] for key in characteristics} == characteristics
    assert sheet.attributes["WOUNDS"] == wounds and sheet.attributes["DAMAGE"] == 0
    assert sheet_value(sheet, pack, "TB") == tb
    catalog = load_item_catalog(pack)
    armour = max(
        (catalog.get(item.profile_id).armor_at("body") for item in sheet.equipment
         if catalog.get(item.profile_id).kind == "armour"),
        default=0,
    )
    # The profile's printed total protection = armour points + TB.
    assert armour + tb == total_body_protection
    assert sheet.npc_type == "troop"


def test_weapons_skills_and_talents_are_materialized_from_the_catalog(pack):
    trooper = _sheet(pack, "desoleum_oathsworn_trooper")
    weapons = {item.profile_id: item for item in trooper.equipment}
    assert set(weapons) == {"lasgun", "knife", "npc_profile_flak_armour"}  # knife: book p.470 default
    assert weapons["lasgun"].current_ammo == 60
    assert trooper.skills["Dodge"] == 1 and trooper.skills["Medicae"] == 2 and trooper.skills["Operate::наземная"] == 2
    assert trooper.talents == ["Быстрая Перезарядка", "Искусный Стук"]
    scum = _sheet(pack, "hive_scum")
    assert [item.profile_id for item in scum.equipment] == ["knife"]
    assert scum.skills["Survival"] == 2
    # «1к5+2БС»: the stated bonus is the sheet's own Strength Bonus.
    assert sheet_value(scum, pack, "SB") == 2


def _patched(monkeypatch, mutate):
    from core.yaml_safety import safe_load_no_aliases

    original = safe_load_no_aliases

    def patched(text):
        data = original(text)
        if isinstance(data, dict) and "profiles" in data:
            mutate(data["profiles"])
        return data

    monkeypatch.setattr(npc_profiles, "safe_load_no_aliases", patched)


def test_a_stated_weapon_that_differs_from_the_catalog_fails_closed(monkeypatch, pack):
    _patched(monkeypatch, lambda profiles: profiles["hired_gun"]["weapons"][0]["stated"].update(rate_of_fire="O/3/10"))
    with pytest.raises(NpcProfileError, match="rate_of_fire"):
        load_npc_profiles(pack)


def test_a_stated_strength_bonus_that_disagrees_with_the_characteristic_fails_closed(monkeypatch, pack):
    _patched(monkeypatch, lambda profiles: profiles["hive_scum"]["attributes"].update(S=45))
    with pytest.raises(NpcProfileError, match="Strength Bonus"):
        load_npc_profiles(pack)


def test_unknown_attributes_and_skills_fail_closed(monkeypatch, pack):
    _patched(monkeypatch, lambda profiles: profiles["hive_scum"]["skills"].update(Flying=1))
    with pytest.raises(NpcProfileError, match="unknown skill"):
        load_npc_profiles(pack)
