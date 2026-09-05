"""Generic, pack-sidecar-driven character-creation layers.

Some systems build a character in several independent stages after the initial
attribute profile: background, role, ancestry, career, faction, etc. These are
not dice-resolution rules and they should not force a giant cross-product of
``creation_constraints.profiles``. This module provides one generic substrate
for such stages.

Built-in sidecars live at ``rulepacks/data/<system>/creation.yaml``. A sidecar
may be split into additional ``creation.d/*.yaml`` fragments so a large system
can keep independent creation stages in auditable files. A user rulepack data
directory may mirror the same layout. An optional ``creation_policy.yaml`` in
the same directory can declare duplicate-replacement pools for list-like fields.
The engine never branches on a concrete system id; ids are only lookup keys for
that system's data directory.

A layer option may apply:

* scalar declared sheet ``fields``;
* list-like ``append_fields``;
* scalar declared sheet ``attributes``;
* rolled sheet attributes through pack-agnostic dice expressions;
* skill ranks, including ``Family::specialization`` keys;
* starting equipment;
* explicit player choice groups, either finite options, a free specialization
  of a declared skill family, or a free specialization interpolated into a
  declared list-like sheet field (for talents/traits/etc.).

Missing choices fail loudly. Pack-declared duplicate replacements also require
an explicit player choice. The substrate never guesses for the player.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.dice_engine import DiceRoller
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


@dataclass(frozen=True)
class CreationDuplicateRequirement:
    field: str
    count: int
    choices: tuple[str, ...]


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
        try:
            import core.rulepacks as rulepacks

            user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
        except Exception:  # pragma: no cover - defensive import boundary
            user_root = None
        if user_root is not None:
            candidates.append(Path(user_root) / "data" / system / "creation.yaml")
        candidates.append(_BUILTIN_DATA_ROOT / system / "creation.yaml")
    return candidates


def _selected_sidecar(pack: Any, data_root: Path | None = None) -> Path | None:
    return next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)


def _load_sidecar_file(path: Path) -> Mapping[str, Any]:
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationLayerError(f"could not load creation sidecar {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationLayerError(f"creation sidecar {path.name!r} root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationLayerError(f"unsupported creation sidecar version in {path.name!r}")
    unknown = set(raw) - {"version", "layers"}
    if unknown:
        raise CreationLayerError(f"creation sidecar {path.name!r} has unknown root keys {sorted(unknown)}")
    layers = raw.get("layers") or {}
    if not isinstance(layers, Mapping):
        raise CreationLayerError(f"creation sidecar {path.name!r}.layers must be a mapping")
    return layers


def load_creation_layers(pack: Any, *, data_root: Path | None = None) -> Mapping[str, Any]:
    """Load a pack's base creation sidecar plus sorted ``creation.d`` fragments."""
    path = _selected_sidecar(pack, data_root)
    if path is None:
        return {}

    merged: dict[str, Any] = {}
    sources = [path]
    fragment_dir = path.parent / "creation.d"
    if fragment_dir.is_dir():
        sources.extend(sorted(fragment_dir.glob("*.yaml")))

    for source in sources:
        layers = _load_sidecar_file(source)
        overlap = set(merged).intersection(str(layer_id) for layer_id in layers)
        if overlap:
            raise CreationLayerError(
                f"creation sidecar {source.name!r} duplicates layer ids {sorted(overlap)}"
            )
        for layer_id, layer in layers.items():
            merged[str(layer_id)] = layer
    return merged


def load_creation_policy(pack: Any, *, data_root: Path | None = None) -> Mapping[str, tuple[str, ...]]:
    """Load optional duplicate-replacement pools from ``creation_policy.yaml``."""
    sidecar = _selected_sidecar(pack, data_root)
    if sidecar is None:
        return {}
    path = sidecar.parent / "creation_policy.yaml"
    if not path.is_file():
        return {}

    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationLayerError(f"could not load creation policy {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationLayerError(f"creation policy {path.name!r} root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationLayerError(f"unsupported creation policy version in {path.name!r}")
    unknown = set(raw) - {"version", "duplicate_replacements"}
    if unknown:
        raise CreationLayerError(f"creation policy {path.name!r} has unknown root keys {sorted(unknown)}")

    duplicate_replacements = raw.get("duplicate_replacements") or {}
    if not isinstance(duplicate_replacements, Mapping):
        raise CreationLayerError("creation policy duplicate_replacements must be a mapping")

    parsed: dict[str, tuple[str, ...]] = {}
    for field_raw, spec_raw in duplicate_replacements.items():
        field_name = str(field_raw).strip()
        if not field_name or not isinstance(spec_raw, Mapping):
            raise CreationLayerError("creation duplicate replacement entries must be field mappings")
        extra = set(spec_raw) - {"choices"}
        if extra:
            raise CreationLayerError(
                f"creation duplicate replacement field {field_name!r} has unknown keys {sorted(extra)}"
            )
        choices = spec_raw.get("choices") or []
        if (
            not isinstance(choices, (list, tuple))
            or not choices
            or not all(isinstance(item, str) and item.strip() for item in choices)
        ):
            raise CreationLayerError(
                f"creation duplicate replacement field {field_name!r}.choices must be a non-empty string list"
            )
        normalized = [_normalize(item) for item in choices]
        if len(set(normalized)) != len(normalized):
            raise CreationLayerError(
                f"creation duplicate replacement field {field_name!r}.choices contains duplicates"
            )
        parsed[field_name] = tuple(str(item).strip() for item in choices)
    return parsed


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


def _list_values(current: Any) -> list[Any]:
    if current in (None, ""):
        return []
    if isinstance(current, list):
        return list(current)
    if isinstance(current, tuple):
        return list(current)
    raise CreationLayerError("append_fields target must be list-like")


def creation_duplicate_requirements(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> tuple[CreationDuplicateRequirement, ...]:
    """Describe unresolved duplicates left intentionally in policy-controlled fields."""

    requirements: list[CreationDuplicateRequirement] = []
    for canonical_field, choices in load_creation_policy(pack, data_root=data_root).items():
        field_name = _declared_field_name(pack, canonical_field)
        values = _list_values(getattr(character, field_name, None))
        if not all(isinstance(item, str) and item.strip() for item in values):
            raise CreationLayerError(
                f"duplicate-replacement field {canonical_field!r} must contain only non-empty strings"
            )
        normalized = [_normalize(item) for item in values]
        duplicate_count = len(normalized) - len(set(normalized))
        if duplicate_count <= 0:
            continue
        present = set(normalized)
        available = tuple(choice for choice in choices if _normalize(choice) not in present)
        if len(available) < duplicate_count:
            raise CreationLayerError(
                f"duplicate-replacement field {canonical_field!r} has too few unused replacement choices"
            )
        requirements.append(
            CreationDuplicateRequirement(
                field=canonical_field,
                count=duplicate_count,
                choices=available,
            )
        )
    return tuple(requirements)


def resolve_creation_duplicates(
    pack: Any,
    character: Any,
    replacements: Mapping[str, Any] | None,
    *,
    data_root: Path | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Atomically replace every deferred duplicate with explicit policy choices."""

    policy = load_creation_policy(pack, data_root=data_root)
    requirements = {item.field: item for item in creation_duplicate_requirements(pack, character, data_root=data_root)}
    queues = _replacement_queues(replacements, policy)
    missing = [field for field in requirements if field not in queues]
    if missing:
        raise CreationLayerError(f"duplicate replacements are required for fields: {', '.join(missing)}")
    extra = set(queues) - set(requirements)
    if extra:
        raise CreationLayerError(f"duplicate replacements were supplied for fields without duplicates: {sorted(extra)}")

    snapshot = copy.deepcopy(vars(character))
    resolved: dict[str, tuple[str, ...]] = {}
    try:
        for canonical_field, requirement in requirements.items():
            queue = queues[canonical_field]
            if len(queue) != requirement.count:
                raise CreationLayerError(
                    f"duplicate-replacement field {canonical_field!r} requires exactly {requirement.count} choices"
                )
            pool = {_normalize(item): item for item in policy[canonical_field]}
            field_name = _declared_field_name(pack, canonical_field)
            values = _list_values(getattr(character, field_name, None))
            original_present = {_normalize(item) for item in values}
            seen_original: set[str] = set()
            selected: set[str] = set()
            chosen: list[str] = []
            rebuilt: list[str] = []
            for value_raw in values:
                value = str(value_raw).strip()
                key = _normalize(value)
                if key not in seen_original:
                    seen_original.add(key)
                    rebuilt.append(value)
                    continue
                replacement_raw = queue.pop(0)
                replacement_key = _normalize(replacement_raw)
                replacement = pool.get(replacement_key)
                if replacement is None:
                    raise CreationLayerError(
                        f"replacement {replacement_raw!r} is not allowed for field {canonical_field!r}"
                    )
                if replacement_key in original_present or replacement_key in selected:
                    raise CreationLayerError(
                        f"replacement {replacement!r} for field {canonical_field!r} is already present"
                    )
                selected.add(replacement_key)
                chosen.append(replacement)
                rebuilt.append(replacement)
            setattr(character, field_name, rebuilt)
            resolved[canonical_field] = tuple(chosen)
        refresh_sheet(character, pack)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise
    return resolved


def _append_unique(current: Any, incoming: list[Any]) -> list[Any]:
    values = _list_values(current)
    for value in incoming:
        if value not in values:
            values.append(value)
    return values


def _replacement_queues(
    raw: Mapping[str, Any] | None,
    policy: Mapping[str, tuple[str, ...]],
) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CreationLayerError("duplicate_replacements must be a mapping of field -> string list")

    queues: dict[str, list[str]] = {}
    for field_raw, values_raw in raw.items():
        field_name = str(field_raw).strip()
        if field_name not in policy:
            raise CreationLayerError(f"field {field_name!r} has no duplicate-replacement policy")
        if (
            not isinstance(values_raw, (list, tuple))
            or not all(isinstance(item, str) and item.strip() for item in values_raw)
        ):
            raise CreationLayerError(f"duplicate_replacements.{field_name} must be a string list")
        queues[field_name] = [str(item).strip() for item in values_raw]
    return queues


def _append_with_policy(
    current: Any,
    incoming: list[Any],
    canonical_field: str,
    policy: Mapping[str, tuple[str, ...]],
    replacement_queues: dict[str, list[str]],
    *,
    defer_duplicates: bool = False,
) -> list[Any]:
    choices = policy.get(canonical_field)
    if choices is None:
        return _append_unique(current, incoming)

    values = _list_values(current)
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise CreationLayerError(
            f"duplicate-replacement field {canonical_field!r} must contain only non-empty strings"
        )
    if not all(isinstance(item, str) and item.strip() for item in incoming):
        raise CreationLayerError(
            f"duplicate-replacement field {canonical_field!r} can append only non-empty strings"
        )

    pool = {_normalize(item): item for item in choices}
    present = {_normalize(item) for item in values}
    queue = replacement_queues.get(canonical_field, [])

    for value_raw in incoming:
        value = str(value_raw).strip()
        normalized = _normalize(value)
        if normalized not in present:
            values.append(value)
            present.add(normalized)
            continue
        if defer_duplicates:
            values.append(value)
            continue
        if not queue:
            raise CreationLayerError(
                f"duplicate value {value!r} for field {canonical_field!r} requires a player replacement choice"
            )
        replacement_raw = queue.pop(0)
        replacement_key = _normalize(replacement_raw)
        replacement = pool.get(replacement_key)
        if replacement is None:
            raise CreationLayerError(
                f"replacement {replacement_raw!r} is not allowed for field {canonical_field!r}"
            )
        if replacement_key in present:
            raise CreationLayerError(
                f"replacement {replacement!r} for field {canonical_field!r} is already present"
            )
        values.append(replacement)
        present.add(replacement_key)

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


def _apply_effects(
    pack: Any,
    character: Any,
    raw: Any,
    roller: DiceRoller,
    *,
    duplicate_policy: Mapping[str, tuple[str, ...]] | None = None,
    replacement_queues: dict[str, list[str]] | None = None,
    defer_duplicates: bool = False,
) -> tuple[list[str], dict[str, int]]:
    if raw is None:
        return [], {}
    if not isinstance(raw, Mapping):
        raise CreationLayerError("creation effects must be a mapping")
    unknown = set(raw) - {"fields", "append_fields", "attributes", "roll_attributes", "skills", "equipment"}
    if unknown:
        raise CreationLayerError(f"creation effects have unknown keys {sorted(unknown)}")

    duplicate_policy = duplicate_policy or {}
    replacement_queues = replacement_queues if replacement_queues is not None else {}

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
        canonical_field = str(canonical)
        field_name = _declared_field_name(pack, canonical_field)
        setattr(
            character,
            field_name,
            _append_with_policy(
                getattr(character, field_name, None),
                list(values),
                canonical_field,
                duplicate_policy,
                replacement_queues,
                defer_duplicates=defer_duplicates,
            ),
        )

    attributes = raw.get("attributes") or {}
    if not isinstance(attributes, Mapping):
        raise CreationLayerError("creation effects.attributes must be a mapping")
    for canonical, value in attributes.items():
        set_sheet_value(character, pack, str(canonical), value)

    rolled = raw.get("roll_attributes") or {}
    if not isinstance(rolled, Mapping):
        raise CreationLayerError("creation effects.roll_attributes must be a mapping")
    for canonical, expression_raw in rolled.items():
        expression = str(expression_raw or "").strip()
        if not expression:
            raise CreationLayerError(f"roll_attributes.{canonical} must be a non-empty dice expression")
        value = roller.roll_expression(expression).total
        set_sheet_value(character, pack, str(canonical), value)

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


def _apply_field_template(pack: Any, character: Any, raw: Any, specialization: Any) -> str:
    """Append one player-specialized value to a declared list-like sheet field."""
    if not isinstance(raw, Mapping):
        raise CreationLayerError("field_template must be a mapping")
    unknown = set(raw) - {"field", "template"}
    if unknown:
        raise CreationLayerError(f"field_template has unknown keys {sorted(unknown)}")
    canonical_field = str(raw.get("field") or "").strip()
    template = str(raw.get("template") or "").strip()
    text = str(specialization or "").strip()
    if not canonical_field or not template:
        raise CreationLayerError("field_template needs field and template")
    if "{specialization}" not in template:
        raise CreationLayerError("field_template.template must contain {specialization}")
    if not text:
        raise CreationLayerError(f"specialization for field {canonical_field!r} is required")
    value = template.replace("{specialization}", text)
    field_name = _declared_field_name(pack, canonical_field)
    setattr(character, field_name, _append_unique(getattr(character, field_name, None), [value]))
    return value


def _apply_choice_group(
    pack: Any,
    character: Any,
    group_id: str,
    group: Mapping[str, Any],
    selection: Any,
    roller: DiceRoller,
    *,
    duplicate_policy: Mapping[str, tuple[str, ...]] | None = None,
    replacement_queues: dict[str, list[str]] | None = None,
    defer_duplicates: bool = False,
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
    unknown = set(option) - {"names", "effects", "skill_family", "rank", "field_template", "display", "rules"}
    if unknown:
        raise CreationLayerError(f"choice option has unknown keys {sorted(unknown)}")
    if "skill_family" in option and "field_template" in option:
        raise CreationLayerError("choice option cannot combine skill_family and field_template")

    equipment, skills = _apply_effects(
        pack,
        character,
        option.get("effects"),
        roller,
        duplicate_policy=duplicate_policy,
        replacement_queues=replacement_queues,
        defer_duplicates=defer_duplicates,
    )
    if "skill_family" in option:
        canonical, rank = _apply_specialization(pack, character, option, specialization)
        skills[canonical] = rank
    elif "field_template" in option:
        _apply_field_template(pack, character, option["field_template"], specialization)
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
    duplicate_replacements: Mapping[str, Any] | None = None,
    defer_duplicates: bool = False,
    data_root: Path | None = None,
    roller: DiceRoller | None = None,
) -> CreationLayerResult:
    """Apply one selected layer option and all explicit player sub-choices.

    By default every duplicate in a policy-controlled list consumes one explicit
    replacement from ``duplicate_replacements[field]``. A higher-level creation
    flow may instead set ``defer_duplicates`` to preserve duplicate values until
    its explicit replacement stage. Both modes remain deterministic and neither
    ever chooses a replacement for the player.
    """
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

    duplicate_policy = load_creation_policy(pack, data_root=data_root)
    if defer_duplicates and duplicate_replacements:
        raise CreationLayerError("deferred duplicate mode cannot also consume replacement choices")
    replacement_queues = {} if defer_duplicates else _replacement_queues(duplicate_replacements, duplicate_policy)
    roller = roller or DiceRoller()

    try:
        snapshot = copy.deepcopy(vars(character))
    except TypeError as exc:
        raise CreationLayerError("character must expose mutable attribute storage") from exc

    try:
        equipment_added, skills_set = _apply_effects(
            pack,
            character,
            raw.get("effects"),
            roller,
            duplicate_policy=duplicate_policy,
            replacement_queues=replacement_queues,
            defer_duplicates=defer_duplicates,
        )
        for group_id_raw, group in choices.items():
            group_id = str(group_id_raw)
            if not isinstance(group, Mapping):
                raise CreationLayerError(f"choice group {group_id!r} must be a mapping")
            equipment, skills = _apply_choice_group(
                pack,
                character,
                group_id,
                group,
                provided[group_id],
                roller,
                duplicate_policy=duplicate_policy,
                replacement_queues=replacement_queues,
                defer_duplicates=defer_duplicates,
            )
            equipment_added.extend(equipment)
            skills_set.update(skills)

        unused = {field: values for field, values in replacement_queues.items() if values}
        if unused:
            fields = ", ".join(sorted(unused))
            raise CreationLayerError(f"unused duplicate replacement choices for fields: {fields}")

        refresh_sheet(character, pack)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise

    return CreationLayerResult(
        layer_id=str(layer_id),
        option_id=option_id,
        selections=provided,
        equipment_added=tuple(equipment_added),
        skills_set=skills_set,
    )
