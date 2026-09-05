"""Generic pack-driven random finalization for character creation.

Some systems end staged character creation with one mandatory random table whose
result may alter characteristics, grant existing sheet fields/skills, require
explicit player sub-choices, or record a persistent rule. The concrete table
lives in ``rulepacks/data/<system>/creation_finalization.yaml``; core only owns
one-shot rolling, declarative effects, choice validation, and persisted state.

Finalization never initializes itself on read and never re-rolls an already
recorded result. A pack may mark a row with ``blocked_reference`` when the row
requires another rule dependency that is not yet represented by pack data; the
rolled result is preserved and creation remains unfinalized instead of silently
inventing or skipping the missing rule.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.creation_flow import creation_flow_status, load_creation_flow_spec
from core.dice_engine import DiceRoller
from core.sheets import refresh_sheet, resolve_skill_family, set_sheet_value, sheet_value
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__creation_finalization__"


class CreationFinalizationError(ValueError):
    """A finalization spec, state transition, or player choice is invalid."""


@dataclass(frozen=True)
class FinalizationRow:
    id: str
    minimum: int
    maximum: int
    display: Mapping[str, str]
    source: str
    rules: Mapping[str, tuple[str, ...]]
    annotations: Mapping[str, Any]
    effects: Mapping[str, Any]
    choices: Mapping[str, Any]
    blocked_reference: str = ""


@dataclass(frozen=True)
class CreationFinalizationSpec:
    roll: str
    rows: tuple[FinalizationRow, ...]


@dataclass(frozen=True)
class CreationFinalizationStatus:
    roll: int
    row_id: str
    selections: Mapping[str, Any]
    complete: bool
    blocked_reference: str = ""


@dataclass(frozen=True)
class CreationFinalizationResult:
    status: CreationFinalizationStatus


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise CreationFinalizationError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise CreationFinalizationError("pack system id is not safe for a finalization path")
    if data_root is not None:
        return [Path(data_root) / system / "creation_finalization.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "creation_finalization.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "creation_finalization.yaml")
    return candidates


def _locale_rules(raw: Any, *, where: str) -> Mapping[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CreationFinalizationError(f"{where} must be a locale mapping")
    parsed: dict[str, tuple[str, ...]] = {}
    for locale, value in raw.items():
        if isinstance(value, str):
            items = (value.strip(),) if value.strip() else ()
        elif isinstance(value, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in value):
            items = tuple(str(item).strip() for item in value)
        else:
            raise CreationFinalizationError(f"{where}.{locale} must be a string or string list")
        parsed[str(locale).casefold()] = items
    return parsed


def load_creation_finalization_spec(
    pack: Any, *, data_root: Path | None = None
) -> CreationFinalizationSpec | None:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationFinalizationError(f"could not load creation-finalization sidecar {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationFinalizationError("creation-finalization sidecar root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationFinalizationError("unsupported creation-finalization sidecar version")
    unknown = set(raw) - {"version", "roll", "rows"}
    if unknown:
        raise CreationFinalizationError(f"creation-finalization sidecar has unknown root keys {sorted(unknown)}")

    roll = str(raw.get("roll") or "").strip()
    rows_raw = raw.get("rows")
    if not roll:
        raise CreationFinalizationError("creation-finalization roll expression is required")
    if not isinstance(rows_raw, (list, tuple)) or not rows_raw:
        raise CreationFinalizationError("creation-finalization rows must be a non-empty list")

    rows: list[FinalizationRow] = []
    ids: set[str] = set()
    occupied: set[int] = set()
    for index, row_raw in enumerate(rows_raw):
        if not isinstance(row_raw, Mapping):
            raise CreationFinalizationError(f"creation-finalization row {index} must be a mapping")
        extra = set(row_raw) - {
            "id",
            "min",
            "max",
            "display",
            "source",
            "rules",
            "annotations",
            "effects",
            "choices",
            "blocked_reference",
        }
        if extra:
            raise CreationFinalizationError(f"creation-finalization row {index} has unknown keys {sorted(extra)}")
        row_id = str(row_raw.get("id") or "").strip()
        if not row_id or row_id in ids:
            raise CreationFinalizationError("creation-finalization row ids must be unique and non-empty")
        ids.add(row_id)
        try:
            minimum = int(row_raw.get("min"))
            maximum = int(row_raw.get("max"))
        except (TypeError, ValueError) as exc:
            raise CreationFinalizationError(f"creation-finalization row {row_id!r} needs integer min/max") from exc
        if minimum > maximum:
            raise CreationFinalizationError(f"creation-finalization row {row_id!r} has min greater than max")
        values = set(range(minimum, maximum + 1))
        if occupied.intersection(values):
            raise CreationFinalizationError(f"creation-finalization row {row_id!r} overlaps another row")
        occupied.update(values)

        display_raw = row_raw.get("display") or {}
        annotations = row_raw.get("annotations") or {}
        effects = row_raw.get("effects") or {}
        choices = row_raw.get("choices") or {}
        if not isinstance(display_raw, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in display_raw.items()
        ):
            raise CreationFinalizationError(f"creation-finalization row {row_id!r}.display must be a string mapping")
        if not isinstance(annotations, Mapping):
            raise CreationFinalizationError(f"creation-finalization row {row_id!r}.annotations must be a mapping")
        if not isinstance(effects, Mapping):
            raise CreationFinalizationError(f"creation-finalization row {row_id!r}.effects must be a mapping")
        if not isinstance(choices, Mapping):
            raise CreationFinalizationError(f"creation-finalization row {row_id!r}.choices must be a mapping")
        blocked_reference = str(row_raw.get("blocked_reference") or "").strip()
        if blocked_reference and choices:
            raise CreationFinalizationError(
                f"creation-finalization row {row_id!r} cannot combine choices with blocked_reference"
            )
        rows.append(
            FinalizationRow(
                id=row_id,
                minimum=minimum,
                maximum=maximum,
                display={str(key).casefold(): str(value) for key, value in display_raw.items()},
                source=str(row_raw.get("source") or "").strip(),
                rules=_locale_rules(row_raw.get("rules"), where=f"row {row_id!r}.rules"),
                annotations=copy.deepcopy(dict(annotations)),
                effects=copy.deepcopy(dict(effects)),
                choices=copy.deepcopy(dict(choices)),
                blocked_reference=blocked_reference,
            )
        )
    return CreationFinalizationSpec(roll=roll, rows=tuple(rows))


def finalization_row(spec: CreationFinalizationSpec, row_id: str) -> FinalizationRow:
    for row in spec.rows:
        if row.id == row_id:
            return row
    raise CreationFinalizationError(f"unknown finalization row {row_id!r}")


def finalization_row_for_roll(spec: CreationFinalizationSpec, roll: int) -> FinalizationRow:
    matches = [row for row in spec.rows if row.minimum <= roll <= row.maximum]
    if len(matches) != 1:
        raise CreationFinalizationError(f"finalization roll {roll} does not resolve to exactly one row")
    return matches[0]


def _state(character: Any) -> dict[str, Any] | None:
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise CreationFinalizationError("character secondary attribute storage must be a mapping")
    raw = secondary.get(_STATE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CreationFinalizationError("creation-finalization state must be a mapping")
    return raw


def _status(character: Any) -> CreationFinalizationStatus | None:
    state = _state(character)
    if state is None:
        return None
    if state.get("version") != 1:
        raise CreationFinalizationError("unsupported creation-finalization state version")
    roll = state.get("roll")
    row_id = state.get("row_id")
    selections = state.get("selections", {})
    complete = state.get("complete")
    blocked_reference = state.get("blocked_reference", "")
    if isinstance(roll, bool) or not isinstance(roll, int):
        raise CreationFinalizationError("creation-finalization state roll is invalid")
    if not isinstance(row_id, str) or not row_id:
        raise CreationFinalizationError("creation-finalization state row id is invalid")
    if not isinstance(selections, Mapping):
        raise CreationFinalizationError("creation-finalization state selections are invalid")
    if not isinstance(complete, bool) or not isinstance(blocked_reference, str):
        raise CreationFinalizationError("creation-finalization state is malformed")
    return CreationFinalizationStatus(
        roll=roll,
        row_id=row_id,
        selections=dict(selections),
        complete=complete,
        blocked_reference=blocked_reference,
    )


def creation_finalization_status(
    pack: Any, character: Any, *, data_root: Path | None = None
) -> CreationFinalizationStatus | None:
    if load_creation_finalization_spec(pack, data_root=data_root) is None:
        return None
    return _status(character)


def _require_creation_complete(pack: Any, character: Any, *, data_root: Path | None) -> None:
    flow_spec = load_creation_flow_spec(pack, data_root=data_root)
    if flow_spec is None:
        return
    flow = creation_flow_status(pack, character, data_root=data_root)
    if flow is None or not flow.complete:
        raise CreationFinalizationError("staged character creation must be complete before finalization")


def _declared_field_name(pack: Any, canonical: str) -> str:
    spec = getattr(pack, "sheet_spec", None)
    field_name = spec.field_keys.get(canonical) if spec is not None else None
    if field_name is None:
        raise CreationFinalizationError(f"finalization effect names undeclared sheet field {canonical!r}")
    return field_name


def _list_field(pack: Any, character: Any, canonical: str) -> tuple[str, list[Any]]:
    field_name = _declared_field_name(pack, canonical)
    current = getattr(character, field_name, None)
    if current in (None, ""):
        values: list[Any] = []
    elif isinstance(current, (list, tuple)):
        values = list(current)
    else:
        raise CreationFinalizationError(f"finalization field {canonical!r} must be list-like")
    return field_name, values


def _append_fields(pack: Any, character: Any, raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise CreationFinalizationError("finalization append_fields must be a mapping")
    for canonical, incoming in raw.items():
        if not isinstance(incoming, (list, tuple)):
            raise CreationFinalizationError(f"append_fields.{canonical} must be a list")
        field_name, values = _list_field(pack, character, str(canonical))
        for value in incoming:
            if value not in values:
                values.append(copy.deepcopy(value))
        setattr(character, field_name, values)


def _apply_skill(pack: Any, character: Any, name: str, rank: Any) -> None:
    try:
        numeric = int(rank)
    except (TypeError, ValueError) as exc:
        raise CreationFinalizationError(f"finalization skill rank for {name!r} must be an integer") from exc
    canonical = str(name).strip()
    family = resolve_skill_family(pack, canonical)
    if family is not None:
        canonical = family[0]
    else:
        canonical = pack.resolve_skill(canonical) or canonical
    set_sheet_value(character, pack, canonical, numeric)


def _predicate(pack: Any, character: Any, raw: Any) -> bool:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise CreationFinalizationError("finalization condition must contain exactly one predicate")
    if "field_contains" in raw:
        spec = raw["field_contains"]
        if not isinstance(spec, Mapping) or set(spec) != {"field", "value"}:
            raise CreationFinalizationError("field_contains needs field and value")
        _field_name, values = _list_field(pack, character, str(spec["field"]))
        wanted = str(spec["value"]).strip().casefold()
        return any(str(value).strip().casefold() == wanted for value in values)
    if "skill_at_least" in raw:
        spec = raw["skill_at_least"]
        if not isinstance(spec, Mapping) or set(spec) != {"skill", "rank"}:
            raise CreationFinalizationError("skill_at_least needs skill and rank")
        name = str(spec["skill"])
        family = resolve_skill_family(pack, name)
        canonical = family[0] if family is not None else (pack.resolve_skill(name) or name)
        return sheet_value(character, pack, canonical) >= int(spec["rank"])
    raise CreationFinalizationError("unknown finalization condition predicate")


def _apply_effects(pack: Any, character: Any, raw: Any, *, depth: int = 0) -> None:
    if raw is None:
        return
    if depth > 4:
        raise CreationFinalizationError("finalization conditional effects are nested too deeply")
    if not isinstance(raw, Mapping):
        raise CreationFinalizationError("finalization effects must be a mapping")
    unknown = set(raw) - {"add_attributes", "append_fields", "skills", "conditional"}
    if unknown:
        raise CreationFinalizationError(f"finalization effects have unknown keys {sorted(unknown)}")

    additions = raw.get("add_attributes") or {}
    if not isinstance(additions, Mapping):
        raise CreationFinalizationError("finalization add_attributes must be a mapping")
    for canonical, delta in additions.items():
        try:
            numeric = int(delta)
        except (TypeError, ValueError) as exc:
            raise CreationFinalizationError(f"finalization attribute delta for {canonical!r} must be an integer") from exc
        current = sheet_value(character, pack, str(canonical))
        set_sheet_value(character, pack, str(canonical), current + numeric)

    _append_fields(pack, character, raw.get("append_fields"))

    skills = raw.get("skills") or {}
    if not isinstance(skills, Mapping):
        raise CreationFinalizationError("finalization skills must be a mapping")
    for skill, rank in skills.items():
        _apply_skill(pack, character, str(skill), rank)

    conditional = raw.get("conditional") or []
    if not isinstance(conditional, (list, tuple)):
        raise CreationFinalizationError("finalization conditional must be a list")
    for index, branch in enumerate(conditional):
        if not isinstance(branch, Mapping) or set(branch) - {"when", "then", "else"}:
            raise CreationFinalizationError(f"finalization conditional[{index}] is malformed")
        if "when" not in branch or "then" not in branch:
            raise CreationFinalizationError(f"finalization conditional[{index}] needs when and then")
        selected = branch.get("then") if _predicate(pack, character, branch["when"]) else branch.get("else")
        _apply_effects(pack, character, selected, depth=depth + 1)


def _resolve_option(group_id: str, options: Mapping[str, Any], selection: str) -> Mapping[str, Any]:
    wanted = " ".join(selection.strip().casefold().replace("_", " ").split())
    found: Mapping[str, Any] | None = None
    for option_id, raw in options.items():
        if not isinstance(raw, Mapping):
            raise CreationFinalizationError(f"finalization choice {group_id!r} contains an invalid option")
        names = raw.get("names") or []
        if not isinstance(names, (list, tuple)) or not all(isinstance(item, str) for item in names):
            raise CreationFinalizationError(f"finalization choice option {option_id!r}.names must be a string list")
        surfaces = (str(option_id), *names)
        if any(" ".join(surface.strip().casefold().replace("_", " ").split()) == wanted for surface in surfaces):
            if found is not None:
                raise CreationFinalizationError(f"ambiguous finalization choice {selection!r}")
            found = raw
    if found is None:
        raise CreationFinalizationError(f"unknown option {selection!r} for finalization choice {group_id!r}")
    return found


def _apply_choice(pack: Any, character: Any, group_id: str, group: Any, selection: Any) -> None:
    if not isinstance(group, Mapping):
        raise CreationFinalizationError(f"finalization choice group {group_id!r} must be a mapping")
    unknown = set(group) - {"options", "field_template"}
    if unknown:
        raise CreationFinalizationError(f"finalization choice group {group_id!r} has unknown keys {sorted(unknown)}")

    if "field_template" in group:
        if "options" in group:
            raise CreationFinalizationError(f"finalization choice group {group_id!r} cannot mix options and field_template")
        template = group["field_template"]
        if not isinstance(template, Mapping):
            raise CreationFinalizationError("finalization field_template must be a mapping")
        extra = set(template) - {"field", "template", "if_present_effects"}
        if extra:
            raise CreationFinalizationError(f"finalization field_template has unknown keys {sorted(extra)}")
        canonical_field = str(template.get("field") or "").strip()
        text_template = str(template.get("template") or "").strip()
        value = str(selection or "").strip()
        if not canonical_field or "{value}" not in text_template or not value:
            raise CreationFinalizationError("finalization field_template needs field, {value} template and selection")
        rendered = text_template.replace("{value}", value)
        field_name, values = _list_field(pack, character, canonical_field)
        if any(str(item).strip().casefold() == rendered.casefold() for item in values):
            _apply_effects(pack, character, template.get("if_present_effects"))
        else:
            values.append(rendered)
            setattr(character, field_name, values)
        return

    options = group.get("options") or {}
    if not isinstance(options, Mapping) or not options:
        raise CreationFinalizationError(f"finalization choice group {group_id!r} needs options or field_template")
    option = _resolve_option(group_id, options, str(selection or ""))
    extra = set(option) - {"names", "display", "effects"}
    if extra:
        raise CreationFinalizationError(f"finalization choice option has unknown keys {sorted(extra)}")
    _apply_effects(pack, character, option.get("effects"))


def _apply_row(pack: Any, character: Any, row: FinalizationRow, selections: Mapping[str, Any] | None) -> dict[str, Any]:
    provided = dict(selections or {})
    missing = [str(group_id) for group_id in row.choices if str(group_id) not in provided]
    if missing:
        raise CreationFinalizationError(f"finalization row {row.id!r} requires choices: {', '.join(missing)}")
    extra = set(provided) - {str(group_id) for group_id in row.choices}
    if extra:
        raise CreationFinalizationError(f"finalization row {row.id!r} got unknown choices {sorted(extra)}")

    _apply_effects(pack, character, row.effects)
    for group_id_raw, group in row.choices.items():
        group_id = str(group_id_raw)
        _apply_choice(pack, character, group_id, group, provided[group_id])
    refresh_sheet(character, pack)
    return provided


def roll_creation_finalization(
    pack: Any,
    character: Any,
    *,
    roller: DiceRoller | None = None,
    data_root: Path | None = None,
) -> CreationFinalizationResult:
    """Roll the pack's finalization table exactly once and auto-apply choice-free rows."""

    spec = load_creation_finalization_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFinalizationError("rulepack has no creation-finalization sidecar")
    _require_creation_complete(pack, character, data_root=data_root)
    if _state(character) is not None:
        raise CreationFinalizationError("character finalization has already been rolled")

    roller = roller or DiceRoller()
    snapshot = copy.deepcopy(vars(character))
    try:
        total = int(roller.roll_expression(spec.roll).total)
        row = finalization_row_for_roll(spec, total)
        state = {
            "version": 1,
            "roll": total,
            "row_id": row.id,
            "selections": {},
            "complete": False,
            "blocked_reference": row.blocked_reference,
        }
        character.secondary_attributes[_STATE_KEY] = state
        if not row.blocked_reference and not row.choices:
            _apply_row(pack, character, row, {})
            state["complete"] = True
        status = _status(character)
        assert status is not None
        return CreationFinalizationResult(status=status)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise


def resolve_creation_finalization(
    pack: Any,
    character: Any,
    selections: Mapping[str, Any] | None,
    *,
    data_root: Path | None = None,
) -> CreationFinalizationResult:
    """Apply explicit choices for an already rolled finalization result."""

    spec = load_creation_finalization_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFinalizationError("rulepack has no creation-finalization sidecar")
    _require_creation_complete(pack, character, data_root=data_root)
    current = _status(character)
    if current is None:
        raise CreationFinalizationError("character finalization has not been rolled")
    if current.complete:
        raise CreationFinalizationError("character finalization is already complete")
    if current.blocked_reference:
        raise CreationFinalizationError("finalization result requires an unresolved referenced rule")
    row = finalization_row(spec, current.row_id)
    if not row.choices:
        raise CreationFinalizationError("finalization result has no player choices to resolve")

    snapshot = copy.deepcopy(vars(character))
    try:
        provided = _apply_row(pack, character, row, selections)
        state = _state(character)
        assert state is not None
        state["selections"] = provided
        state["complete"] = True
        status = _status(character)
        assert status is not None
        return CreationFinalizationResult(status=status)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise
