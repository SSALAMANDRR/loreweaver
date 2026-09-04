"""Generic pack-declared character-creation profiles.

A profile is one named creation option inside ``creation_constraints.profiles``.
It may override attribute roll expressions, set sheet attributes / meta fields,
and declare deterministic bonus rolls whose success condition is a safe
``core.condexpr`` expression. The engine does not know what a profile means:
home worlds, ancestries, careers, templates, and similar rule-system concepts
all use the same data shape.

This module intentionally stays separate from ``CharacterManager``'s legacy
``generate_character`` entry point. Callers that need a selected profile use
``generate_profiled_character``; the command/UI wiring can choose a profile
without teaching storage or the rulepack registry any system-specific words.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.character_manager import CharacterSheet
from core.condexpr import CondExprError, compile_expression, truthy
from core.dice_engine import DiceRoller
from core.sheets import canonical_values, refresh_sheet, set_sheet_value, sheet_value


class CreationProfileError(ValueError):
    """A pack declared an invalid or unknown creation profile."""


@dataclass(frozen=True)
class CreationProfileResult:
    """One completed profile generation plus the exact random inputs used."""

    character: CharacterSheet
    profile_id: str
    attribute_rolls: Mapping[str, int]
    bonus_rolls: Mapping[str, int]


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _profiles(pack: Any) -> Mapping[str, Any]:
    raw = (getattr(pack, "creation_constraints", {}) or {}).get("profiles") or {}
    if not isinstance(raw, Mapping):
        raise CreationProfileError("creation_constraints.profiles must be a mapping")
    return raw


def resolve_creation_profile(pack: Any, name: str) -> tuple[str, Mapping[str, Any]] | None:
    """Resolve a profile id or one of its declared ``names`` aliases."""

    wanted = _normalize(name)
    if not wanted:
        return None

    found: tuple[str, Mapping[str, Any]] | None = None
    for profile_id_raw, raw in _profiles(pack).items():
        profile_id = str(profile_id_raw).strip()
        if not profile_id or not isinstance(raw, Mapping):
            raise CreationProfileError("every creation profile needs a non-empty id and mapping body")
        aliases = raw.get("names") or []
        if not isinstance(aliases, (list, tuple)) or not all(isinstance(item, str) for item in aliases):
            raise CreationProfileError(f"creation profile {profile_id!r}.names must be a string list")
        surfaces = (profile_id, *aliases)
        if any(_normalize(surface) == wanted for surface in surfaces):
            if found is not None and found[0] != profile_id:
                raise CreationProfileError(f"creation profile alias {name!r} is ambiguous")
            found = (profile_id, raw)
    return found


def _mapping(profile_id: str, raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, Mapping):
        raise CreationProfileError(f"creation profile {profile_id!r}.{key} must be a mapping")
    return value


def _apply_fields(character: CharacterSheet, pack: Any, profile_id: str, values: Mapping[str, Any]) -> None:
    spec = getattr(pack, "sheet_spec", None)
    for canonical, value in values.items():
        canonical_name = str(canonical)
        field_name = spec.field_keys.get(canonical_name) if spec is not None else None
        if field_name is None:
            raise CreationProfileError(
                f"creation profile {profile_id!r} names undeclared sheet field {canonical_name!r}"
            )
        setattr(character, field_name, value)


def _apply_attributes(character: CharacterSheet, pack: Any, values: Mapping[str, Any]) -> None:
    for canonical, value in values.items():
        try:
            numeric = int(value)
        except (TypeError, ValueError) as exc:
            raise CreationProfileError(f"creation profile attribute {canonical!r} must be numeric") from exc
        set_sheet_value(character, pack, str(canonical), numeric)


def _store_rolled_attribute(character: CharacterSheet, pack: Any, canonical: str, value: int) -> None:
    """Store one rolled canonical attribute without forcing a derived refresh per die.

    Most packs use identical canonical/storage names, but a sheet may map a
    canonical attribute such as ``Wounds`` to a storage key such as ``WOUNDS``.
    Profile roll overrides are allowed to introduce such mapped attributes even
    when they are not part of the base characteristic-generation table.
    """

    spec = getattr(pack, "sheet_spec", None)
    storage_key = spec.attr_keys.get(canonical, canonical) if spec is not None else canonical
    character.attributes[storage_key] = value


def _bonus_success(pack: Any, character: CharacterSheet, roll: int, expression: str) -> bool:
    try:
        compiled = compile_expression(expression)
    except CondExprError as exc:
        raise CreationProfileError(f"bad creation bonus condition {expression!r}: {exc}") from exc

    namespace = canonical_values(character, pack)
    namespace.update(pack.compute_derived(namespace))
    namespace["roll"] = roll

    def resolve(path: str) -> Any:
        if path == "roll":
            return roll
        return namespace.get(path, getattr(pack, "defaults", {}).get(path, 0))

    try:
        return truthy(compiled(resolve))
    except CondExprError as exc:
        raise CreationProfileError(f"could not evaluate creation bonus condition {expression!r}: {exc}") from exc


def generate_profiled_character(
    pack: Any,
    profile: str,
    name: str = "",
    *,
    roller: DiceRoller | None = None,
) -> CreationProfileResult:
    """Generate one fresh sheet using a selected pack-declared profile.

    Base attribute rolls come from ``creation_constraints.attributes[*].roll``.
    A profile's ``attribute_rolls`` mapping replaces the expression for selected
    attributes and may add another declared sheet attribute whose starting value
    is rolled by the profile. ``attributes`` and ``fields`` then write
    deterministic starting values. Finally each ``bonus_rolls`` row rolls once
    and, when its safe ``when`` expression succeeds, applies
    ``add_attributes`` / ``set_attributes``.

    Supported profile keys are deliberately small and closed; unknown keys fail
    loudly instead of becoming silently ignored house rules.
    """

    resolved = resolve_creation_profile(pack, profile)
    if resolved is None:
        raise CreationProfileError(f"unknown creation profile: {profile}")
    profile_id, raw = resolved

    allowed = {"names", "attribute_rolls", "attributes", "fields", "bonus_rolls"}
    unknown = set(raw) - allowed
    if unknown:
        raise CreationProfileError(f"creation profile {profile_id!r} has unknown keys {sorted(unknown)}")

    roller = roller or DiceRoller()
    character = CharacterSheet(name=name, system=pack.system)
    constraints = getattr(pack, "creation_constraints", {}) or {}
    attribute_rules = constraints.get("attributes") or {}
    if not isinstance(attribute_rules, Mapping):
        raise CreationProfileError("creation_constraints.attributes must be a mapping")
    overrides = _mapping(profile_id, raw, "attribute_rolls")

    attribute_rolls: dict[str, int] = {}
    keys = list(attribute_rules)
    keys.extend(key for key in overrides if key not in attribute_rules)
    for key_raw in keys:
        key = str(key_raw)
        rule = attribute_rules.get(key_raw)
        base_expression = rule.get("roll") if isinstance(rule, Mapping) else None
        expression = overrides.get(key_raw, overrides.get(key, base_expression))
        if expression is None:
            continue
        if not isinstance(expression, str) or not expression.strip():
            raise CreationProfileError(f"creation roll for {key!r} must be a non-empty dice expression")
        total = roller.roll_expression(expression.strip()).total
        _store_rolled_attribute(character, pack, key, total)
        attribute_rolls[key] = total

    _apply_attributes(character, pack, _mapping(profile_id, raw, "attributes"))
    _apply_fields(character, pack, profile_id, _mapping(profile_id, raw, "fields"))

    bonus_rows = raw.get("bonus_rolls") or []
    if not isinstance(bonus_rows, (list, tuple)):
        raise CreationProfileError(f"creation profile {profile_id!r}.bonus_rolls must be a list")
    bonus_rolls: dict[str, int] = {}
    for index, row in enumerate(bonus_rows):
        if not isinstance(row, Mapping):
            raise CreationProfileError(f"creation profile {profile_id!r}.bonus_rolls[{index}] must be a mapping")
        row_unknown = set(row) - {"id", "roll", "when", "add_attributes", "set_attributes"}
        if row_unknown:
            raise CreationProfileError(
                f"creation profile {profile_id!r}.bonus_rolls[{index}] has unknown keys {sorted(row_unknown)}"
            )
        roll_id = str(row.get("id") or f"bonus_{index + 1}").strip()
        expression = str(row.get("roll") or "").strip()
        condition = str(row.get("when") or "").strip()
        if not roll_id or not expression or not condition:
            raise CreationProfileError(
                f"creation profile {profile_id!r}.bonus_rolls[{index}] needs id, roll and when"
            )
        total = roller.roll_expression(expression).total
        bonus_rolls[roll_id] = total
        if not _bonus_success(pack, character, total, condition):
            continue

        additions = row.get("add_attributes") or {}
        if not isinstance(additions, Mapping):
            raise CreationProfileError(f"creation bonus {roll_id!r}.add_attributes must be a mapping")
        for canonical, delta in additions.items():
            current = sheet_value(character, pack, str(canonical))
            set_sheet_value(character, pack, str(canonical), current + int(delta))

        replacements = row.get("set_attributes") or {}
        if not isinstance(replacements, Mapping):
            raise CreationProfileError(f"creation bonus {roll_id!r}.set_attributes must be a mapping")
        _apply_attributes(character, pack, replacements)

    refresh_sheet(character, pack, initialize_vitals=True, preserve_trained=False)
    return CreationProfileResult(
        character=character,
        profile_id=profile_id,
        attribute_rolls=attribute_rolls,
        bonus_rolls=bonus_rolls,
    )
