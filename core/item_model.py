"""Generic, pack-driven typed item profiles and character item instances.

``ItemProfile`` contains immutable rule data owned by a rulepack.  ``ItemInstance``
contains only the identity and mutable state of one item owned by a character.  The
module deliberately stops at representation and validation; it does not resolve
attacks, reactions, damage, or weapon qualities.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_ALLOWED_KINDS = {"weapon", "armour", "gear", "consumable", "ammunition"}
_ALLOWED_COMPLETENESS = {"complete", "source_incomplete"}


class ItemModelError(ValueError):
    """An item profile or instance is malformed."""


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _string_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ItemModelError(f"{where} must be a string list")
    return tuple(item.strip() for item in value)


def _optional_int(value: Any, *, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ItemModelError(f"{where} must be an integer or null")  # i18n-exempt: internal validation diagnostic
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ItemModelError(f"{where} must be an integer or null") from exc  # i18n-exempt: internal validation diagnostic


def _rate_of_fire(value: Any, *, where: str) -> dict[str, int | None]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ItemModelError(f"{where} must be a mapping")
    result: dict[str, int | None] = {}
    for mode, shots in value.items():
        mode_name = str(mode).strip()
        if not mode_name:
            raise ItemModelError(f"{where} has an empty fire mode")
        result[mode_name] = _optional_int(shots, where=f"{where}.{mode_name}")
        if result[mode_name] is not None and result[mode_name] < 1:
            raise ItemModelError(f"{where}.{mode_name} must be positive or null")
    return result


def _armor_by_location(value: Any, *, where: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ItemModelError(f"{where} must be a mapping")
    result: dict[str, int] = {}
    for location, armor in value.items():
        name = str(location).strip()
        if not name:
            raise ItemModelError(f"{where} has an empty location")
        parsed = _optional_int(armor, where=f"{where}.{name}")
        if parsed is None or parsed < 0:
            raise ItemModelError(f"{where}.{name} must be a non-negative integer")  # i18n-exempt: internal validation diagnostic
        result[name] = parsed
    return result


@dataclass(frozen=True)
class ItemProfile:
    """Static item data supplied by a rulepack."""

    id: str
    name: str
    aliases: tuple[str, ...]
    kind: str
    source_reference: str
    completeness: str = "complete"
    availability: int | None = None
    damage_expression: str | None = None
    damage_type: str | None = None
    penetration: int | None = None
    range: int | str | None = None
    rate_of_fire: Mapping[str, int | None] = field(default_factory=dict)
    clip_size: int | None = None
    reload: str | None = None
    qualities: tuple[str, ...] = ()
    ammo_requirements: tuple[str, ...] = ()
    armor_by_location: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ItemModelError("item profile id and name are required")  # i18n-exempt: internal validation diagnostic
        if self.kind not in _ALLOWED_KINDS:
            raise ItemModelError(f"item profile kind must be one of {sorted(_ALLOWED_KINDS)}")  # i18n-exempt: internal validation diagnostic
        if not self.source_reference.strip():
            raise ItemModelError("item profile source_reference is required")  # i18n-exempt: internal validation diagnostic
        if self.completeness not in _ALLOWED_COMPLETENESS:
            raise ItemModelError(
                f"item profile completeness must be one of {sorted(_ALLOWED_COMPLETENESS)}"  # i18n-exempt: internal validation diagnostic
            )
        if self.availability is not None and self.availability < -100:
            raise ItemModelError("item profile availability is out of range")  # i18n-exempt: internal validation diagnostic
        if self.penetration is not None and self.penetration < 0:
            raise ItemModelError("item profile penetration must be non-negative")  # i18n-exempt: internal validation diagnostic
        if self.clip_size is not None and self.clip_size < 1:
            raise ItemModelError("item profile clip_size must be positive")  # i18n-exempt: internal validation diagnostic
        object.__setattr__(self, "rate_of_fire", MappingProxyType(dict(self.rate_of_fire)))
        object.__setattr__(self, "armor_by_location", MappingProxyType(dict(self.armor_by_location)))

    @property
    def is_usable_for_resolution(self) -> bool:
        """Whether the source has enough fields for a future resolver to use it."""

        if self.completeness != "complete":
            return False
        if self.kind != "weapon":
            return True
        # Range, RoF, clip, and reload are intentionally optional here: they are
        # not applicable to melee weapons. A future action resolver will validate
        # the applicable subset for its chosen action kind.
        return bool(self.damage_expression and self.damage_type and self.penetration is not None)

    def armor_at(self, location: str) -> int:
        """Return the static armour value for a hit location, or zero if uncovered."""

        return int(self.armor_by_location.get(str(location).strip(), 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "aliases": list(self.aliases),
            "kind": self.kind,
            "source_reference": self.source_reference,
            "completeness": self.completeness,
            "availability": self.availability,
            "damage_expression": self.damage_expression,
            "damage_type": self.damage_type,
            "penetration": self.penetration,
            "range": self.range,
            "rate_of_fire": dict(self.rate_of_fire),
            "clip_size": self.clip_size,
            "reload": self.reload,
            "qualities": list(self.qualities),
            "ammo_requirements": list(self.ammo_requirements),
            "armor_by_location": dict(self.armor_by_location),
        }


@dataclass
class ItemInstance:
    """Mutable state for one character-owned item."""

    instance_id: str
    profile_id: str
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ItemModelError("item instance_id is required")
        if not self.profile_id.strip():
            raise ItemModelError("item instance profile_id is required")  # i18n-exempt: internal validation diagnostic
        if not isinstance(self.state, dict):
            raise ItemModelError("item instance state must be a mapping")  # i18n-exempt: internal validation diagnostic
        self.state = copy.deepcopy(self.state)
        if "current_ammo" in self.state:
            ammo = self.state["current_ammo"]
            if isinstance(ammo, bool) or not isinstance(ammo, int) or ammo < 0:
                raise ItemModelError("item instance current_ammo must be a non-negative integer")  # i18n-exempt: internal validation diagnostic

    @property
    def current_ammo(self) -> int | None:
        value = self.state.get("current_ammo")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "profile_id": self.profile_id,
            "state": copy.deepcopy(self.state),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ItemInstance:
        if not isinstance(data, Mapping):
            raise ItemModelError("item instance must be a mapping")  # i18n-exempt: internal validation diagnostic
        unknown = set(data) - {"instance_id", "profile_id", "state"}
        if unknown:
            raise ItemModelError(f"item instance has unknown keys {sorted(unknown)}")
        state = data.get("state") or {}
        if not isinstance(state, Mapping):
            raise ItemModelError("item instance state must be a mapping")  # i18n-exempt: internal validation diagnostic
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            profile_id=str(data.get("profile_id") or ""),
            state=dict(state),
        )

    @classmethod
    def create(cls, profile: ItemProfile, *, state: Mapping[str, Any] | None = None) -> ItemInstance:
        return cls(
            instance_id=f"{profile.id}-{uuid.uuid4().hex}",
            profile_id=profile.id,
            state=dict(state or {}),
        )


class ItemProfileCatalog:
    """Validated profile lookup by id, name, or alias."""

    def __init__(self, profiles: Mapping[str, ItemProfile], *, source: str = "") -> None:
        self.profiles = dict(profiles)
        self.source = source
        self._surfaces: dict[str, ItemProfile] = {}
        for profile in self.profiles.values():
            for surface in (profile.id, profile.name, *profile.aliases):
                key = _normalize(surface)
                previous = self._surfaces.get(key)
                if previous is not None and previous.id != profile.id:
                    raise ItemModelError(
                        f"item profile alias {surface!r} is claimed by both {previous.id!r} and {profile.id!r}"
                    )
                self._surfaces[key] = profile

    def get(self, profile_id: str) -> ItemProfile | None:
        return self.profiles.get(str(profile_id))

    def resolve(self, target: str) -> ItemProfile:
        profile = self._surfaces.get(_normalize(target))
        if profile is None:
            raise ItemModelError(f"unknown item profile {target!r}")
        return profile


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise ItemModelError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise ItemModelError("pack system id is not safe for an item-profile path")  # i18n-exempt: internal validation diagnostic
    if data_root is not None:
        return [Path(data_root) / system / "items.yaml"]
    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "items.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "items.yaml")
    return candidates


def _parse_profile(profile_id_raw: Any, raw: Any) -> ItemProfile:
    profile_id = str(profile_id_raw or "").strip()
    if not profile_id or not isinstance(raw, Mapping):
        raise ItemModelError("item profile entries must be named mappings")  # i18n-exempt: internal validation diagnostic
    unknown = set(raw) - {
        "name",
        "aliases",
        "kind",
        "source_reference",
        "completeness",
        "availability",
        "damage_expression",
        "damage_type",
        "penetration",
        "range",
        "rate_of_fire",
        "clip_size",
        "reload",
        "qualities",
        "ammo_requirements",
        "armor_by_location",
    }
    if unknown:
        raise ItemModelError(f"item profile {profile_id!r} has unknown keys {sorted(unknown)}")
    name = str(raw.get("name") or "").strip()
    aliases = _string_tuple(raw.get("aliases"), where=f"item profile {profile_id!r}.aliases")
    kind = str(raw.get("kind") or "").strip().casefold()
    source_reference = str(raw.get("source_reference") or "").strip()
    range_value = raw.get("range")
    if range_value is not None and not isinstance(range_value, (int, str)):
        raise ItemModelError(f"item profile {profile_id!r}.range must be an integer, string, or null")  # i18n-exempt: internal validation diagnostic
    return ItemProfile(
        id=profile_id,
        name=name,
        aliases=aliases,
        kind=kind,
        source_reference=source_reference,
        completeness=str(raw.get("completeness") or "complete").strip(),
        availability=_optional_int(raw.get("availability"), where=f"item profile {profile_id!r}.availability"),
        damage_expression=(str(raw["damage_expression"]).strip() if raw.get("damage_expression") is not None else None),
        damage_type=(str(raw["damage_type"]).strip() if raw.get("damage_type") is not None else None),
        penetration=_optional_int(raw.get("penetration"), where=f"item profile {profile_id!r}.penetration"),
        range=range_value,
        rate_of_fire=_rate_of_fire(raw.get("rate_of_fire"), where=f"item profile {profile_id!r}.rate_of_fire"),
        clip_size=_optional_int(raw.get("clip_size"), where=f"item profile {profile_id!r}.clip_size"),
        reload=(str(raw["reload"]).strip() if raw.get("reload") is not None else None),
        qualities=_string_tuple(raw.get("qualities"), where=f"item profile {profile_id!r}.qualities"),
        ammo_requirements=_string_tuple(
            raw.get("ammo_requirements"), where=f"item profile {profile_id!r}.ammo_requirements"
        ),
        armor_by_location=_armor_by_location(
            raw.get("armor_by_location"), where=f"item profile {profile_id!r}.armor_by_location"
        ),
    )


def load_item_catalog(pack: Any, *, data_root: Path | None = None) -> ItemProfileCatalog | None:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ItemModelError(f"could not load item profiles {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ItemModelError("item profiles root must be a mapping")  # i18n-exempt: internal validation diagnostic
    if int(raw.get("version", 1)) != 1:
        raise ItemModelError("unsupported item profiles version")
    unknown = set(raw) - {"version", "source", "profiles"}
    if unknown:
        raise ItemModelError(f"item profiles has unknown keys {sorted(unknown)}")
    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, Mapping) or not profiles_raw:
        raise ItemModelError("item profiles must be a non-empty mapping")  # i18n-exempt: internal validation diagnostic
    profiles = {str(profile_id): _parse_profile(profile_id, entry) for profile_id, entry in profiles_raw.items()}
    return ItemProfileCatalog(profiles, source=str(raw.get("source") or "").strip())


def serialize_equipment_entry(entry: Any) -> Any:
    if isinstance(entry, ItemInstance):
        return entry.to_dict()
    return entry


def deserialize_equipment_entry(entry: Any) -> Any:
    if isinstance(entry, ItemInstance):
        return entry
    if isinstance(entry, Mapping) and {"instance_id", "profile_id"}.issubset(entry):
        return ItemInstance.from_dict(entry)
    return entry


def equipment_entry_label(entry: Any, catalog: ItemProfileCatalog | None = None) -> str:
    if isinstance(entry, ItemInstance):
        profile = catalog.get(entry.profile_id) if catalog is not None else None
        return profile.name if profile is not None else entry.profile_id
    return str(entry)
