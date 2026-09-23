"""Pack-declared opponent profiles, materialized into ordinary character sheets.

A profile is rulepack data (`rulepacks/data/<system>/npc_profiles.yaml`): the
characteristics, skills, talent/trait labels, armour and weapons a published stat
block states. Materialization produces a plain `CharacterSheet` carrying typed
`ItemInstance`s from the pack's item catalog, so the existing combat resolver runs
the NPC exactly like a player character. The loader fails closed: a weapon whose
stated profile row disagrees with the catalog row it would be built from is
rejected rather than silently given the catalog's capabilities.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.character_manager import CharacterSheet
from core.item_model import ItemInstance, ItemProfile, load_item_catalog
from core.yaml_safety import safe_load_no_aliases

_BONUS_SUFFIX = re.compile(r"^(?P<dice>.+?)\+(?P<bonus>\d+)\s*БС$")


class NpcProfileError(ValueError):
    """The profile data is invalid or cannot be materialized faithfully."""


@dataclass(frozen=True)
class NpcProfile:
    id: str
    name: str
    npc_type: str
    source_reference: str
    attributes: Mapping[str, int]
    skills: Mapping[str, int] = field(default_factory=dict)
    talents: tuple[str, ...] = ()
    traits: tuple[str, ...] = ()
    armour: tuple[str, ...] = ()
    weapons: tuple[Mapping[str, Any], ...] = ()
    aliases: tuple[str, ...] = ()
    omitted: tuple[str, ...] = ()


def _data_path(pack: Any) -> Path:
    return Path(__file__).resolve().parent.parent / "rulepacks" / "data" / str(pack.system) / "npc_profiles.yaml"


def _labels(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(value, str) and value.strip() for value in raw):
        raise NpcProfileError(f"{where} must be a list of names")  # i18n-exempt: internal validation diagnostic
    return tuple(value.strip() for value in raw)


def _int_map(raw: Any, where: str) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in raw.values()
    ):
        raise NpcProfileError(f"{where} must map names to integers")  # i18n-exempt: internal validation diagnostic
    return {str(key): int(value) for key, value in raw.items()}


def _rate_of_fire(stated: str) -> dict[str, int | None]:
    """`O/3/–` notation: single available (O) or not (–), then semi and full shot counts."""
    parts = [part.strip() for part in str(stated).split("/")]
    if len(parts) != 3:
        raise NpcProfileError(f"unsupported rate-of-fire notation {stated!r}")  # i18n-exempt: internal validation diagnostic

    def shots(part: str) -> int | None:
        return None if part in {"–", "-", "—"} else int(part)

    return {"single": 1 if parts[0] in {"O", "О"} else None, "semi": shots(parts[1]), "full": shots(parts[2])}


def _check_weapon(profile_id: str, item: ItemProfile, stated: Mapping[str, Any], strength_bonus: int) -> None:
    """Compare the fields the resolver consumes; a mismatch means the catalog row is not this weapon."""
    where = f"npc profile {profile_id!r} weapon {item.id!r}"
    damage = str(stated.get("damage") or "")
    bonus = _BONUS_SUFFIX.match(damage)
    if bonus is not None:
        if int(bonus.group("bonus")) != strength_bonus:
            raise NpcProfileError(f"{where}: stated SB damage does not match the materialized Strength Bonus")  # i18n-exempt: internal validation diagnostic
        damage = bonus.group("dice")
    mismatches = []
    if damage != item.damage_expression:
        mismatches.append("damage")
    if stated.get("penetration") != item.penetration:
        mismatches.append("penetration")
    if "range" in stated and stated["range"] != item.range:
        mismatches.append("range")
    if "rate_of_fire" in stated and {
        mode: shots for mode, shots in _rate_of_fire(stated["rate_of_fire"]).items()
    } != {mode: item.rate_of_fire.get(mode) for mode in ("single", "semi", "full")}:
        mismatches.append("rate_of_fire")
    if "clip" in stated and stated["clip"] != item.clip_size:
        mismatches.append("clip")
    if "reload" in stated and stated["reload"] != item.reload:
        mismatches.append("reload")
    if mismatches:
        raise NpcProfileError(f"{where}: stated profile differs from the item catalog ({', '.join(mismatches)})")  # i18n-exempt: internal validation diagnostic


def load_npc_profiles(pack: Any) -> dict[str, NpcProfile]:
    """Every profile the pack declares (empty when it declares none)."""
    path = _data_path(pack)
    if not path.is_file():
        return {}
    raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping) or int(raw.get("version", 1)) != 1:
        raise NpcProfileError("unsupported npc profile data")  # i18n-exempt: internal validation diagnostic
    spec = pack.sheet_spec
    if spec is None:
        raise NpcProfileError("rulepack has no sheet spec")  # i18n-exempt: internal validation diagnostic
    catalog = load_item_catalog(pack)
    default_melee = raw.get("default_melee_weapon")
    profiles: dict[str, NpcProfile] = {}
    for profile_id, entry in (raw.get("profiles") or {}).items():
        where = f"npc profile {profile_id!r}"
        if not isinstance(entry, Mapping) or not entry.get("name") or not entry.get("npc_type"):
            raise NpcProfileError(f"{where} needs a name and an npc_type")  # i18n-exempt: internal validation diagnostic
        attributes = _int_map(entry.get("attributes"), f"{where}.attributes")
        unknown = sorted(set(attributes) - set(spec.attr_keys))
        if unknown:
            raise NpcProfileError(f"{where}: unknown attributes {unknown}")  # i18n-exempt: internal validation diagnostic
        skills = _int_map(entry.get("skills"), f"{where}.skills")
        for skill in skills:
            family = skill.split("::", 1)[0] if "::" in skill else None
            if skill not in spec.skills and family not in spec.skill_families:
                raise NpcProfileError(f"{where}: unknown skill {skill!r}")  # i18n-exempt: internal validation diagnostic
        armour = _labels(entry.get("armour"), f"{where}.armour")
        weapons = tuple(entry.get("weapons") or ())
        if catalog is None and (armour or weapons):
            raise NpcProfileError(f"{where}: the pack has no item catalog")  # i18n-exempt: internal validation diagnostic
        for armour_id in armour:
            item = catalog.get(armour_id)
            if item is None or item.kind != "armour":
                raise NpcProfileError(f"{where}: unknown armour {armour_id!r}")  # i18n-exempt: internal validation diagnostic
        strength_bonus = attributes.get("S", 0) // 10
        for weapon in weapons:
            item = catalog.get(str(weapon.get("profile") or "")) if isinstance(weapon, Mapping) else None
            if item is None or item.kind != "weapon" or not item.is_usable_for_resolution:
                raise NpcProfileError(f"{where}: unusable weapon {weapon!r}")  # i18n-exempt: internal validation diagnostic
            _check_weapon(str(profile_id), item, weapon.get("stated") or {}, strength_bonus)
        if default_melee and default_melee not in {weapon["profile"] for weapon in weapons}:
            if catalog.get(str(default_melee)) is None:
                raise NpcProfileError(f"unknown default melee weapon {default_melee!r}")  # i18n-exempt: internal validation diagnostic
            weapons = (*weapons, {"profile": str(default_melee)})
        profiles[str(profile_id)] = NpcProfile(
            id=str(profile_id),
            name=str(entry["name"]),
            npc_type=str(entry["npc_type"]),
            source_reference=str(entry.get("source_reference") or ""),
            attributes=attributes,
            skills=skills,
            talents=_labels(entry.get("talents"), f"{where}.talents"),
            traits=_labels(entry.get("traits"), f"{where}.traits"),
            armour=armour,
            weapons=weapons,
            aliases=_labels(entry.get("aliases"), f"{where}.aliases"),
            omitted=_labels(entry.get("omitted"), f"{where}.omitted"),
        )
    return profiles


def find_npc_profile(profiles: Mapping[str, NpcProfile], query: str) -> NpcProfile | None:
    wanted = query.strip().casefold()
    for profile in profiles.values():
        if wanted in {profile.id.casefold(), profile.name.casefold(), *(alias.casefold() for alias in profile.aliases)}:
            return profile
    return None


def materialize_npc_sheet(profile: NpcProfile, name: str, pack: Any) -> CharacterSheet:
    """A validated, combat-ready sheet built only from the profile's stated values."""
    from core.character_rules import validate_sheet

    spec = pack.sheet_spec
    catalog = load_item_catalog(pack)
    sheet = CharacterSheet(name.strip(), pack.system)
    if not sheet.name:
        raise NpcProfileError("an NPC needs a name")  # i18n-exempt: internal validation diagnostic
    for canonical, value in profile.attributes.items():
        sheet.attributes[spec.attr_keys[canonical]] = value
    for skill, rank in profile.skills.items():
        sheet.skills[spec.skill_keys.get(skill, skill)] = rank
    for canonical, value in (
        ("NpcType", profile.npc_type),
        ("Talents", list(profile.talents)),
        ("Traits", list(profile.traits)),
    ):
        key = spec.field_keys.get(canonical)
        if key is None:
            raise NpcProfileError(f"rulepack declares no {canonical} sheet field")  # i18n-exempt: internal validation diagnostic
        setattr(sheet, key, value)
    equipment: list[ItemInstance] = []
    for weapon in profile.weapons:
        item = catalog.resolve(str(weapon["profile"]))
        equipment.append(ItemInstance.create(item, state={"current_ammo": item.clip_size} if item.clip_size else None))
    equipment.extend(ItemInstance.create(catalog.resolve(armour_id)) for armour_id in profile.armour)
    sheet.equipment = equipment
    validated, violations = validate_sheet(sheet, pack.system, initialize_vitals=True)
    if violations:
        raise NpcProfileError(f"npc profile {profile.id!r} violates sheet constraints")  # i18n-exempt: internal validation diagnostic
    return validated
