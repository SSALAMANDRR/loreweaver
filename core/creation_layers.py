"""Generic, pack-sidecar-driven character-creation layers.

Some systems build a character in several independent stages after the initial
attribute profile: background, role, ancestry, career, faction, etc.  These are
not dice-resolution rules and they should not force a giant cross-product of
``creation_constraints.profiles``.  This module provides one generic substrate
for such stages.

Built-in sidecars live at ``rulepacks/data/<system>/creation.yaml``.  A user
rulepack directory may mirror the same ``data/<system>/creation.yaml`` layout.
The engine never branches on a concrete system id; the id is only the lookup
key for that system's data directory.

A layer option may apply:

* scalar declared sheet ``fields``;
* list-like ``append_fields``;
* skill ranks, including ``Family::specialization`` keys;
* starting equipment;
* explicit player choice groups, either finite options or a free
  specialization of a declared skill family.

Missing choices fail loudly.  The substrate never guesses for the player.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.sheets import refresh_sheet, resolve_skill_family, set_sheet_value
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"


class CreationLayerError(ValueError):
    """A layered-creation sidecar or requested choice is invalid."""


@dataclass(frozen=True)
class CreationLayerResult:
    layer_id: str
    option_id: str
    selections: Mapping[str, Any]
    equipment_added: tuple[str, ...]
    skills_set: Mapping[str, int]


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise CreationLayerError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise CreationLayerError("pack system id is not safe for a sidecar path")

    candidates: list[Path] = []
    if data_root is not None:
        candidates.append(Path(data_root) / system / "creation.yaml")
    else:
        # User rulepack data wins when present, matching the user-rulepack override
        # philosophy without importing the private path at module import time.
        try:
            import core.rulepacks as rulepacks

            user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
        except Exception:  # pragma: no cover - defensive import boundary
            user_root = None
        if user_root is not None:
            candidates.append(Path(user_root) / "data" / system / "creation.yaml")
        candidates.append(_BUILTIN_DATA_ROOT / system / "creation.yaml")
    return candidates


def load_creation_layers(pack: Any, *, data_root: Path | None = None) -> Mapping[str, Any]:
    """Load and minimally validate a pack's optional layered-creation sidecar."""
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return {}
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationLayerError(f"could not load creation sidecar: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationLayerError("creation sidecar root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationLayerError("unsupported creation sidecar version")
    unknown = set(raw) - {"version", "layers"}
    if unknown:
        raise CreationLayerError(f"creation sidecar has unknown root keys {sorted(unknown)}")
    layers = raw.get("layers") or {}
    if not isinstance(layers, Mapping):
        raise CreationLayerError("creation sidecar layers must be a mapping")
    return layers


def _resolve_named_option(options: Mapping[str, Any], name: str, where: str) -> tuple[str, Mapping[str, Any]] | None:
    wanted = _normalize(name)
    if not wanted:
        return None
    found: tuple[str, Mapping[str, Any]] | None = None
    for option_id_raw, raw in options.items():
        option_id = str(option_id_raw).strip()
        if not option_id or not isinstance(raw, Mapping):
            raise CreationLayerError(f"{where} contains an invalid option")
        aliases = raw.get("names") or []
        if not isinstance(aliases, (list, tuple)) or not all(isinstance(item, str) for item in aliases):
            raise CreationLayerError(f"{where}.{option_id}.names must be a string list")
        if any(_normalize(surface) == wanted for surface in (option_id, *aliases)):
            if found is not None and found[0] != option_id:
                raise CreationLayerError(f"ambiguous creation option alias {name!r}")
            found = (option_id, raw)
    return found


def resolve_creation_layer_option(
    pack: Any,
    layer_id: str,
    name: str,
    *,
    data_root: Path | None = None,
) -> tuple[str, Mapping[str, Any]] | None:
    layers = load_creation_layers(pack, data_root=data_root)
    layer = layers.get(layer_id)
    if not isinstance(layer, Mapping):
        return None
    options = layer.get("options") or {}
    if not isinstance(options, Mapping):
        raise CreationLayerError(f"creation layer {layer_id!r}.options must be a mapping")
    return _resolve_named_option(options, name, f"creation layer {layer_id!r}")


def _declared_field_name(pack: Any, canonical: str) -> str:
    spec = getattr(pack, "sheet_spec", None)
    field_name = spec.field_keys.get(canonical) if spec is not None else None
    if field_name is None:
        raise CreationLayerError(f"creation effect names undeclared sheet field {canonical!r}")
    return field_name


def _append_unique(current: Any, incoming: list[Any]) -> list[Any]:
    if current in (None, ""):
        values: list[Any] = []
    elif isinstance(current, list):
        values = list(current)
    elif isinstance(current, tuple):
        values = list(current)
    else:
        raise CreationLayerError("append_fields target must be list-like")
    for value in incoming:
        if value not in values:
            values.append(value)
    return values


def _apply_skill(pack: Any, character: Any, name: str, rank: Any) -> tuple[str, int]:
    try:
        numeric_rank = int(rank)
    except (TypeError, ValueError) as exc:
        raise CreationLayerError(f"skill rank for {name!r} must be an integer") from exc
    canonical = str(name).strip()
    family = resolve_skill_family(pack, canonical)
    if family is None:
        canonical = pack.resolve_skill(canonical) or canonical
    else:
        canonical = family[0]
    set_sheet_value(character, pack, canonical, numeric_rank)
    return canonical, numeric_rank


def _apply_effects(pack: Any, character: Any, raw: Any) -> tuple[list[str], dict[str, int]]:
    if raw is None:
        return [], {}
    if not isinstance(raw, Mapping):
        raise CreationLayerError("creation effects must be a mapping")
    unknown = set(raw) - {"fields", "append_fields", "skills", "equipment"}
    if unknown:
        raise CreationLayerError(f"creation effects have unknown keys {sorted(unknown)}")

    fields = raw.get("fields") or {}
    if not isinstance(fields, Mapping):
        raise CreationLayerError("creation effects.fields must be a mapping")
    for canonical, value in fields.items():
        setattr(character, _declared_field_name(pack, str(canonical)), value)

    appended = raw.get("append_fields") or {}
    if not isinstance(appended, Mapping):
        raise CreationLayerError("creation effects.append_fields must be a mapping")
    for canonical, values in appended.items():
        if not isinstance(values, (list, tuple)):
            raise CreationLayerError(f"append_fields.{canonical} must be a list")
        field_name = _declared_field_name(pack, str(canonical))
        setattr(character, field_name, _append_unique(getattr(character, field_name, None), list(values)))

    skills = raw.get("skills") or {}
    if not isinstance(skills, Mapping):
        raise CreationLayerError("creation effects.skills must be a mapping")
    applied_skills: dict[str, int] = {}
    for skill, rank in skills.items():
        canonical, numeric_rank = _apply_skill(pack, character, str(skill), rank)
        applied_skills[canonical] = numeric_rank

    equipment = raw.get("equipment") or []
    if not isinstance(equipment, (list, tuple)) or not all(isinstance(item, str) for item in equipment):
        raise CreationLayerError("creation effects.equipment must be a string list")
    added_equipment: list[str] = []
    current_equipment = getattr(character, "equipment", None)
    if not isinstance(current_equipment, list):
        raise CreationLayerError("character equipment storage is not a list")
    for item in equipment:
        if item not in current_equipment:
            current_equipment.append(item)
            added_equipment.append(item)

    return added_equipment, applied_skills


def _apply_specialization(pack: Any, character: Any, group: Mapping[str, Any], specialization: Any) -> tuple[str, int]:
    family_id = str(group.get("skill_family") or "").strip()
    if not family_id:
        raise CreationLayerError("specialization choice needs skill_family")
    text = str(specialization or "").strip()
    if not text:
        raise CreationLayerError(f"specialization for {family_id} is required")
    try:
        rank = int(group.get("rank", 1))
    except (TypeError, ValueError) as exc:
        raise CreationLayerError("specialization rank must be an integer") from exc
    canonical, numeric_rank = _apply_skill(pack, character, f"{family_id}::{text}", rank)
    if resolve_skill_family(pack, canonical) is None:
        raise CreationLayerError(f"unknown skill family {family_id!r}")
    return canonical, numeric_rank


def _apply_choice_group(
    pack: Any,
    character: Any,
    group_id: str,
    group: Mapping[str, Any],
    selection: Any,
) -> tuple[list[str], dict[str, int]]:
    if "skill_family" in group and "options" not in group:
        canonical, rank = _apply_specialization(pack, character, group, selection)
        return [], {canonical: rank}

    options = group.get("options") or {}
    if not isinstance(options, Mapping) or not options:
        raise CreationLayerError(f"choice group {group_id!r} needs options or skill_family")

    if isinstance(selection, Mapping):
        option_name = str(selection.get("option") or "")
        specialization = selection.get("specialization")
        extra = set(selection) - {"option", "specialization"}
        if extra:
            raise CreationLayerError(f"choice {group_id!r} has unknown selection keys {sorted(extra)}")
    else:
        option_name = str(selection or "")
        specialization = None

    resolved = _resolve_named_option(options, option_name, f"choice group {group_id!r}")
    if resolved is None:
        raise CreationLayerError(f"unknown option {option_name!r} for choice {group_id!r}")
    _option_id, option = resolved
    unknown = set(option) - {"names", "effects", "skill_family", "rank", "display", "rules"}
    if unknown:
        raise CreationLayerError(f"choice option has unknown keys {sorted(unknown)}")

    equipment, skills = _apply_effects(pack, character, option.get("effects"))
    if "skill_family" in option:
        canonical, rank = _apply_specialization(pack, character, option, specialization)
        skills[canonical] = rank
    elif specialization not in (None, ""):
        raise CreationLayerError(f"choice {group_id!r} does not accept a specialization")
    return equipment, skills


def apply_creation_layer(
    pack: Any,
    character: Any,
    layer_id: str,
    option: str,
    *,
    selections: Mapping[str, Any] | None = None,
    data_root: Path | None = None,
) -> CreationLayerResult:
    """Apply one selected layer option and all explicit player sub-choices."""
    layers = load_creation_layers(pack, data_root=data_root)
    layer = layers.get(layer_id)
    if not isinstance(layer, Mapping):
        raise CreationLayerError(f"unknown creation layer: {layer_id}")
    options = layer.get("options") or {}
    if not isinstance(options, Mapping):
        raise CreationLayerError(f"creation layer {layer_id!r}.options must be a mapping")
    resolved = _resolve_named_option(options, option, f"creation layer {layer_id!r}")
    if resolved is None:
        raise CreationLayerError(f"unknown {layer_id} option: {option}")
    option_id, raw = resolved
    unknown = set(raw) - {"names", "display", "source", "rules", "effects", "choices"}
    if unknown:
        raise CreationLayerError(f"creation option {option_id!r} has unknown keys {sorted(unknown)}")

    choices = raw.get("choices") or {}
    if not isinstance(choices, Mapping):
        raise CreationLayerError(f"creation option {option_id!r}.choices must be a mapping")
    provided = dict(selections or {})
    missing = [str(group_id) for group_id in choices if str(group_id) not in provided]
    if missing:
        raise CreationLayerError(f"creation option {option_id!r} requires choices: {', '.join(missing)}")
    extra = set(provided) - {str(group_id) for group_id in choices}
    if extra:
        raise CreationLayerError(f"creation option {option_id!r} got unknown choices {sorted(extra)}")

    equipment_added, skills_set = _apply_effects(pack, character, raw.get("effects"))
    for group_id_raw, group in choices.items():
        group_id = str(group_id_raw)
        if not isinstance(group, Mapping):
            raise CreationLayerError(f"choice group {group_id!r} must be a mapping")
        equipment, skills = _apply_choice_group(pack, character, group_id, group, provided[group_id])
        equipment_added.extend(equipment)
        skills_set.update(skills)

    refresh_sheet(character, pack)
    return CreationLayerResult(
        layer_id=str(layer_id),
        option_id=option_id,
        selections=provided,
        equipment_added=tuple(equipment_added),
        skills_set=skills_set,
    )
