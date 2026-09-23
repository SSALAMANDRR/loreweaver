from pathlib import Path

import pytest

from core.character_manager import CharacterSheet
from core.item_model import (
    ItemInstance,
    ItemModelError,
    load_item_catalog,
)
from core.rulepacks import load_rulepack

REPRESENTATIVE_PROFILES = {
    "lasgun",
    "autogun",
    "laspistol",
    "sword",
    "knife",
    "frag_grenade",
    "basic_flak_armor",
}
NPC_PROFILE_ITEMS = {"npc_profile_flak_armour"}


def test_dh2_representative_profiles_parse_and_retain_canonical_source_references():
    catalog = load_item_catalog(load_rulepack("dh2"))

    assert catalog is not None
    assert set(catalog.profiles) == REPRESENTATIVE_PROFILES | NPC_PROFILE_ITEMS
    assert all(catalog.get(profile_id).source_reference.startswith("CH05_") for profile_id in REPRESENTATIVE_PROFILES)
    # Rows sourced from NPC profile stat blocks cite those pages, not Chapter V.
    npc_armour = catalog.get("npc_profile_flak_armour")
    assert "Глава XII с. 486" in npc_armour.source_reference
    assert all(npc_armour.armor_at(location) == 4 for location in ("head", "body", "left_arm", "right_leg"))

    lasgun = catalog.resolve("Lasgun")
    assert lasgun.damage_expression == "1к10+3"
    assert lasgun.damage_type == "E"
    assert lasgun.penetration == 0
    assert lasgun.range == 100
    assert lasgun.rate_of_fire == {"single": 1, "semi": 3, "full": None}
    assert lasgun.clip_size == 60
    assert lasgun.reload == "full"
    assert lasgun.qualities == ("Надёжное",)
    assert lasgun.ammo_requirements == ("standard_las_battery",)
    assert lasgun.source_reference == "CH05_H076"

    autogun = catalog.resolve("Автоган")
    assert autogun.rate_of_fire["full"] == 10
    assert autogun.clip_size == 30
    assert autogun.source_reference == "CH05_H078"


def test_profiles_with_missing_neon_table_rows_are_explicitly_incomplete():
    catalog = load_item_catalog(load_rulepack("dh2"))
    assert catalog is not None

    for profile_id, source in (("laspistol", "CH05_H071"), ("frag_grenade", "CH05_H104")):
        profile = catalog.resolve(profile_id)
        assert profile.completeness == "source_incomplete"
        assert not profile.is_usable_for_resolution
        assert profile.source_reference == source
        assert profile.damage_expression is None


def test_profile_lookup_supports_id_name_and_alias():
    catalog = load_item_catalog(load_rulepack("dh2"))
    assert catalog is not None

    expected = catalog.resolve("basic_flak_armor")
    assert catalog.resolve("Флак-жилет") is expected
    assert catalog.resolve("Flak Vest") is expected
    assert catalog.resolve("flak_vest") is expected


def test_armor_profile_looks_up_value_by_hit_location():
    catalog = load_item_catalog(load_rulepack("dh2"))
    assert catalog is not None

    armor = catalog.resolve("basic_flak_armor")
    assert armor.kind == "armour"
    assert armor.armor_by_location == {"body": 3}
    assert armor.armor_at("body") == 3
    assert armor.armor_at("head") == 0
    assert armor.source_reference == "CH05_H187"


def test_item_instance_serializes_with_only_profile_identity_and_mutable_state():
    original = ItemInstance(
        instance_id="lasgun-001",
        profile_id="lasgun",
        state={"current_ammo": 17, "magazines": 2},
    )

    payload = original.to_dict()
    restored = ItemInstance.from_dict(payload)

    assert payload == {
        "instance_id": "lasgun-001",
        "profile_id": "lasgun",
        "state": {"current_ammo": 17, "magazines": 2},
    }
    assert restored == original
    assert restored.current_ammo == 17

    sheet = CharacterSheet("Acolyte", "dh2")
    sheet.equipment = [original]
    round_trip = CharacterSheet.from_dict(sheet.to_dict())
    assert round_trip.equipment == [original]


def test_item_instance_rejects_invalid_ammo_state():
    with pytest.raises(ItemModelError, match="current_ammo"):
        ItemInstance(instance_id="lasgun-001", profile_id="lasgun", state={"current_ammo": -1})


def test_profile_loader_rejects_invalid_static_profile_data(tmp_path: Path):
    data_dir = tmp_path / "demo"
    data_dir.mkdir()
    (data_dir / "items.yaml").write_text(
        """
version: 1
profiles:
  broken:
    name: Broken
    kind: weapon
    source_reference: TEST_001
    clip_size: 0
""".strip(),
        encoding="utf-8",
    )

    class Pack:
        system = "demo"

    with pytest.raises(ItemModelError, match="clip_size"):
        load_item_catalog(Pack(), data_root=tmp_path)


# --- legacy label upgrade (sheets saved before creation issued typed items) --------------


def _legacy_sheet():
    return {
        "name": "Кардел",
        "system": "dh2",
        "attributes": {},
        "equipment": ["флак-броня Астра Милитарум", "лазган", "Цепной клинок", "Флак-жилет"],
    }


def test_a_legacy_sheet_upgrades_only_catalog_labels_and_keeps_the_rest():
    sheet = CharacterSheet.from_dict(_legacy_sheet())
    kinds = [(type(entry).__name__, getattr(entry, "profile_id", entry)) for entry in sheet.equipment]
    assert kinds == [
        ("str", "флак-броня Астра Милитарум"),
        ("ItemInstance", "lasgun"),
        ("str", "Цепной клинок"),
        ("ItemInstance", "basic_flak_armor"),
    ]
    lasgun = sheet.equipment[1]
    assert lasgun.current_ammo == 60  # one issued clip in the weapon
    assert sheet.equipment[3].current_ammo is None  # armour carries no ammunition state


def test_the_upgrade_is_deterministic_until_a_save_persists_it():
    first = CharacterSheet.from_dict(_legacy_sheet())
    second = CharacterSheet.from_dict(_legacy_sheet())
    assert first.equipment[1].instance_id == second.equipment[1].instance_id
    other = CharacterSheet.from_dict({**_legacy_sheet(), "name": "Someone Else"})
    assert other.equipment[1].instance_id != first.equipment[1].instance_id
    round_trip = CharacterSheet.from_dict(first.to_dict())
    assert round_trip.equipment[1].instance_id == first.equipment[1].instance_id


def test_a_legacy_pc_now_gets_combat_actions():
    from core.combat import create_combat_state
    from gateway.combat_actions import available_actions

    pack = load_rulepack("dh2")
    sheet = CharacterSheet.from_dict(_legacy_sheet())
    state = create_combat_state([sheet.name, "Культист"], current_actor=sheet.name, pack=pack)
    actions = {action["id"] for action in available_actions(pack, sheet, ["Культист"], "ru", state)}
    assert {"ranged_attack", "aim"} <= actions
