"""Generic pack-driven starting-equipment acquisitions.

Some systems grant a fixed number of free item selections during character
creation. The allowance is explicitly initialized on a fresh character so an
old/campaign sheet can never mint creation gear merely by opening a UI later.
The item catalog and all thresholds live in ``starting_equipment.yaml``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.sheets import sheet_value
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__starting_equipment__"
_ALLOWED_KINDS = {"weapon", "armour", "gear", "consumable", "ammunition"}


class StartingEquipmentError(ValueError):
    """Starting-equipment rules, state or a requested acquisition are invalid."""


@dataclass(frozen=True)
class StartingItem:
    id: str
    name: str
    aliases: tuple[str, ...]
    availability: int
    kind: str
    uses_standard_magazines: bool = False
    source: str = ""


@dataclass(frozen=True)
class StartingEquipmentSpec:
    count_stat: str
    minimum_availability: int
    weapon_magazines: int
    items: Mapping[str, StartingItem]
    source: str = ""


@dataclass(frozen=True)
class StartingEquipmentBudget:
    total: int
    used: int
    remaining: int


@dataclass(frozen=True)
class StartingEquipmentGrant:
    item: StartingItem
    equipment_added: tuple[str, ...]
    budget: StartingEquipmentBudget


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise StartingEquipmentError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise StartingEquipmentError("pack system id is not safe for a starting-equipment path")
    if data_root is not None:
        return [Path(data_root) / system / "starting_equipment.yaml"]
    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "starting_equipment.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "starting_equipment.yaml")
    return candidates


def _int(value: Any, *, where: str) -> int:
    if isinstance(value, bool):
        raise StartingEquipmentError(f"{where} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StartingEquipmentError(f"{where} must be an integer") from exc


def load_starting_equipment_spec(
    pack: Any, *, data_root: Path | None = None
) -> StartingEquipmentSpec | None:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise StartingEquipmentError(f"could not load starting-equipment catalog {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise StartingEquipmentError("starting-equipment catalog root must be a mapping")
    if _int(raw.get("version", 1), where="starting-equipment version") != 1:
        raise StartingEquipmentError("unsupported starting-equipment catalog version")
    unknown = set(raw) - {
        "version",
        "count_stat",
        "minimum_availability",
        "weapon_magazines",
        "source",
        "items",
    }
    if unknown:
        raise StartingEquipmentError(f"starting-equipment catalog has unknown keys {sorted(unknown)}")

    count_stat = str(raw.get("count_stat") or "").strip()
    if not count_stat:
        raise StartingEquipmentError("starting-equipment count_stat is required")
    minimum = _int(raw.get("minimum_availability"), where="minimum_availability")
    weapon_magazines = _int(raw.get("weapon_magazines", 0), where="weapon_magazines")
    if weapon_magazines < 0:
        raise StartingEquipmentError("weapon_magazines must be non-negative")

    items_raw = raw.get("items")
    if not isinstance(items_raw, Mapping) or not items_raw:
        raise StartingEquipmentError("starting-equipment items must be a non-empty mapping")
    items: dict[str, StartingItem] = {}
    claimed: dict[str, str] = {}
    for item_id_raw, entry in items_raw.items():
        item_id = str(item_id_raw).strip()
        if not item_id or not isinstance(entry, Mapping):
            raise StartingEquipmentError("starting-equipment item entries must be named mappings")
        unknown_item = set(entry) - {
            "name",
            "aliases",
            "availability",
            "kind",
            "uses_standard_magazines",
            "source",
        }
        if unknown_item:
            raise StartingEquipmentError(
                f"starting-equipment item {item_id!r} has unknown keys {sorted(unknown_item)}"
            )
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("kind") or "").strip().casefold()
        if not name:
            raise StartingEquipmentError(f"starting-equipment item {item_id!r}.name is required")
        if kind not in _ALLOWED_KINDS:
            raise StartingEquipmentError(
                f"starting-equipment item {item_id!r}.kind must be one of {sorted(_ALLOWED_KINDS)}"
            )
        aliases_raw = entry.get("aliases") or []
        if not isinstance(aliases_raw, (list, tuple)) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases_raw
        ):
            raise StartingEquipmentError(f"starting-equipment item {item_id!r}.aliases must be a string list")
        aliases = tuple(str(alias).strip() for alias in aliases_raw)
        availability = _int(entry.get("availability"), where=f"item {item_id!r}.availability")
        uses_standard_magazines = entry.get("uses_standard_magazines", False)
        if not isinstance(uses_standard_magazines, bool):
            raise StartingEquipmentError(
                f"starting-equipment item {item_id!r}.uses_standard_magazines must be boolean"
            )
        if uses_standard_magazines and kind != "weapon":
            raise StartingEquipmentError(
                f"starting-equipment item {item_id!r} may use standard magazines only when kind=weapon"
            )

        for surface in (item_id, name, *aliases):
            key = _normalize(surface)
            previous = claimed.get(key)
            if previous is not None and previous != item_id:
                raise StartingEquipmentError(
                    f"starting-equipment alias {surface!r} is claimed by both {previous!r} and {item_id!r}"
                )
            claimed[key] = item_id

        items[item_id] = StartingItem(
            id=item_id,
            name=name,
            aliases=aliases,
            availability=availability,
            kind=kind,
            uses_standard_magazines=uses_standard_magazines,
            source=str(entry.get("source") or "").strip(),
        )

    return StartingEquipmentSpec(
        count_stat=count_stat,
        minimum_availability=minimum,
        weapon_magazines=weapon_magazines,
        items=items,
        source=str(raw.get("source") or "").strip(),
    )


def _state(character: Any) -> dict[str, Any] | None:
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise StartingEquipmentError("character secondary attribute storage must be a mapping")
    raw = secondary.get(_STATE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StartingEquipmentError("starting-equipment state must be a mapping")
    return raw


def _budget_from_state(state: Mapping[str, Any]) -> StartingEquipmentBudget:
    total = _int(state.get("total", 0), where="starting-equipment total")
    selections = state.get("selections", [])
    if total < 0 or not isinstance(selections, list) or not all(isinstance(item, Mapping) for item in selections):
        raise StartingEquipmentError("starting-equipment state is malformed")
    used = len(selections)
    if used > total:
        raise StartingEquipmentError("starting-equipment state spends more selections than its allowance")
    return StartingEquipmentBudget(total=total, used=used, remaining=total - used)


def initialize_starting_equipment(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
    force: bool = False,
) -> StartingEquipmentBudget | None:
    """Freeze a fresh character's creation-item allowance from the pack stat."""

    spec = load_starting_equipment_spec(pack, data_root=data_root)
    if spec is None:
        return None
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise StartingEquipmentError("character secondary attribute storage must be a mapping")
    if _STATE_KEY in secondary and not force:
        state = _state(character)
        assert state is not None
        return _budget_from_state(state)
    total = max(0, sheet_value(character, pack, spec.count_stat))
    secondary[_STATE_KEY] = {"total": total, "selections": []}
    return StartingEquipmentBudget(total=total, used=0, remaining=total)


def starting_equipment_budget(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> StartingEquipmentBudget | None:
    if load_starting_equipment_spec(pack, data_root=data_root) is None:
        return None
    state = _state(character)
    if state is None:
        return None
    return _budget_from_state(state)


def _resolve_item(spec: StartingEquipmentSpec, target: str) -> StartingItem:
    wanted = _normalize(target)
    if not wanted:
        raise StartingEquipmentError("starting-equipment item name is required")
    matches = [
        item
        for item in spec.items.values()
        if any(_normalize(surface) == wanted for surface in (item.id, item.name, *item.aliases))
    ]
    if not matches:
        raise StartingEquipmentError(f"unknown starting-equipment item {target!r}")
    if len(matches) > 1:
        raise StartingEquipmentError(f"ambiguous starting-equipment item {target!r}")
    return matches[0]


def available_starting_items(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> tuple[StartingItem, ...]:
    spec = load_starting_equipment_spec(pack, data_root=data_root)
    if spec is None or starting_equipment_budget(pack, character, data_root=data_root) is None:
        return ()
    return tuple(
        item for item in spec.items.values() if item.availability >= spec.minimum_availability
    )


def choose_starting_item(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> StartingEquipmentGrant:
    """Atomically consume one initialized creation slot and grant one legal item."""

    spec = load_starting_equipment_spec(pack, data_root=data_root)
    if spec is None:
        raise StartingEquipmentError(f"rulepack {getattr(pack, 'system', '')!r} has no starting-equipment catalog")
    state = _state(character)
    if state is None:
        raise StartingEquipmentError("character starting-equipment allowance is not initialized")
    budget = _budget_from_state(state)
    if budget.remaining <= 0:
        raise StartingEquipmentError("character has no starting-equipment selections remaining")
    item = _resolve_item(spec, target)
    if item.availability < spec.minimum_availability:
        raise StartingEquipmentError(
            f"item {item.name!r} availability {item.availability} is below the starting limit "
            f"{spec.minimum_availability}"
        )

    equipment = getattr(character, "equipment", None)
    if not isinstance(equipment, list):
        raise StartingEquipmentError("character equipment storage is not a list")

    snapshot = copy.deepcopy(vars(character))
    try:
        added = [item.name]
        equipment.append(item.name)
        if item.uses_standard_magazines and spec.weapon_magazines:
            ammo = f"Стандартные боеприпасы: {item.name} ({spec.weapon_magazines} магазина)"
            equipment.append(ammo)
            added.append(ammo)

        selections = state.get("selections")
        if not isinstance(selections, list):
            raise StartingEquipmentError("starting-equipment selections must be a list")
        selections.append(
            {
                "id": item.id,
                "name": item.name,
                "availability": item.availability,
                "kind": item.kind,
                "grants": list(added),
            }
        )
        new_budget = _budget_from_state(state)
        return StartingEquipmentGrant(item=item, equipment_added=tuple(added), budget=new_budget)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise
