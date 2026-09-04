"""The generic sheet substrate (M16 stage B): pack-declared sheet shapes.

A rulepack's ``sheet:`` section declares everything about how its system's
character sheets are SHAPED — the fresh-sheet tables, the canonical-name <->
storage-key bridge, which slots are pure derivations (recomputed on read,
never trusted from storage), the current-pool vitals and their creation
initialization, generic skill-family templates, and the generic ``resources``
list the wire/panels render. The engine holds none of it: every function here
reads the spec.

The derived pipeline is ``source -> (modifier layer) -> derived`` — the
modifier layer is the reserved empty insertion point (`RulePack
.compute_derived`); `refresh_sheet` applies the derived halves onto a sheet's
storage slots, which is what "derived values are NEVER persisted" means in
practice: storage may carry stale copies, readers always overwrite them from
the pack DAG before use, and `strip_derived` drops them from what gets saved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_RESOURCES = 8
_SKILL_FAMILY_SEPARATOR = "::"
_SKILL_FAMILY_RE = re.compile(r"^\s*(.+?)\s*[（(]\s*(.+?)\s*[）)]\s*$")
_SPACE_RE = re.compile(r"\s+")


class SheetSpecError(ValueError):
    """A malformed ``sheet:`` section (raised at pack load time)."""


class UntrainedSkillError(ValueError):
    """A specialized skill family forbids checks without that specialization."""

    def __init__(self, skill: str):
        super().__init__(f"specialized skill is not trained: {skill}")
        self.skill = skill


@dataclass(frozen=True)
class VitalSpec:
    """One current pool: its max slot and how creation initializes it."""

    key: str
    max_key: str
    start: Any


@dataclass(frozen=True)
class ResourceSpec:
    """One wire/panel resource meter."""

    id: str
    labels: Mapping[str, str]
    value_key: str = ""
    max_key: str = ""
    source: str = "attributes"

    def label_for(self, locale: str | None) -> str:
        short = (locale or "en").split("-", 1)[0].split("_", 1)[0]
        for candidate in (short, "en"):
            text = self.labels.get(candidate)
            if text:
                return text
        return next((text for text in self.labels.values() if text), self.id)


@dataclass(frozen=True)
class SkillFamilySpec:
    """A wildcard family of separately stored skills.

    ``Family (specialization)`` resolves to one canonical storage key
    ``Family::specialization``. Each specialization owns its own rank. ``ranks``
    maps stored ranks to target modifiers, while ``untrained_modifier=None``
    means an absent specialization cannot be checked at all.
    """

    base: str
    aliases: tuple[str, ...]
    ranks: Mapping[int, int]
    untrained_modifier: int | None = None


@dataclass(frozen=True)
class SheetSpec:
    """One pack's declared sheet shape."""

    label: str
    attr_keys: Mapping[str, str] = field(default_factory=dict)
    skill_keys: Mapping[str, str] = field(default_factory=dict)
    secondary_keys: Mapping[str, str] = field(default_factory=dict)
    field_keys: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    skills: Mapping[str, Any] = field(default_factory=dict)
    secondary: Mapping[str, Any] = field(default_factory=dict)
    fields: Mapping[str, Any] = field(default_factory=dict)
    hit_points: Mapping[str, int] | None = None
    derived_skills: Mapping[str, str] = field(default_factory=dict)
    derived_attrs: Mapping[str, str] = field(default_factory=dict)
    derived_secondary: Mapping[str, str] = field(default_factory=dict)
    check_values: Mapping[str, str] = field(default_factory=dict)
    skill_families: Mapping[str, SkillFamilySpec] = field(default_factory=dict)
    vitals: tuple[VitalSpec, ...] = ()
    resources: tuple[ResourceSpec, ...] = ()

    def key_to_canonical(self) -> dict[str, str]:
        return {key: canonical for canonical, key in self.attr_keys.items()}


def _str_map(pack_id: str, where: str, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SheetSpecError(f"rulepack '{pack_id}': {where} must be a mapping")
    return {str(key): str(value) for key, value in raw.items()}


def _any_map(pack_id: str, where: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SheetSpecError(f"rulepack '{pack_id}': {where} must be a mapping")
    return {str(key): value for key, value in raw.items()}


def _resource_labels(pack_id: str, resource_id: Any, raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.resource {resource_id} has an empty label")
        return {"en": text}
    if isinstance(raw, Mapping):
        labels = {str(locale): str(text).strip() for locale, text in raw.items() if str(text).strip()}
        if not labels:
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.resource {resource_id} has an empty label map")
        return labels
    raise SheetSpecError(f"rulepack '{pack_id}': sheet.resource {resource_id} label must be a string or locale map")


def _normalize_skill_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip().casefold().replace("_", " "))


def _parse_skill_families(pack_id: str, raw: Any) -> dict[str, SkillFamilySpec]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families must be a mapping")

    families: dict[str, SkillFamilySpec] = {}
    claimed_aliases: dict[str, str] = {}
    for family_id_raw, entry in raw.items():
        family_id = str(family_id_raw).strip()
        if not family_id:
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families has an empty family id")
        if _SKILL_FAMILY_SEPARATOR in family_id:
            raise SheetSpecError(f"rulepack '{pack_id}': skill family id {family_id!r} may not contain '{_SKILL_FAMILY_SEPARATOR}'")
        if not isinstance(entry, Mapping):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id} must be a mapping")
        unknown = set(entry) - {"base", "aliases", "ranks", "untrained"}
        if unknown:
            raise SheetSpecError(
                f"rulepack '{pack_id}': sheet.skill_families.{family_id} has unknown keys {sorted(unknown)}"
            )

        base = str(entry.get("base") or "").strip()
        if not base:
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id}.base is required")

        aliases_raw = entry.get("aliases") or []
        if not isinstance(aliases_raw, (list, tuple)) or not all(isinstance(alias, str) for alias in aliases_raw):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id}.aliases must be a string list")
        aliases = tuple(str(alias).strip() for alias in aliases_raw if str(alias).strip())

        ranks_raw = entry.get("ranks")
        if not isinstance(ranks_raw, Mapping) or not ranks_raw:
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id}.ranks must be a non-empty mapping")
        ranks: dict[int, int] = {}
        for rank_raw, modifier_raw in ranks_raw.items():
            try:
                rank = int(rank_raw)
                modifier = int(modifier_raw)
            except (TypeError, ValueError) as exc:
                raise SheetSpecError(
                    f"rulepack '{pack_id}': sheet.skill_families.{family_id}.ranks must map integer ranks to integer modifiers"
                ) from exc
            if rank < 1:
                raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id}.ranks start at 1")
            ranks[rank] = modifier

        untrained_raw = entry.get("untrained", "forbidden")
        if isinstance(untrained_raw, str) and untrained_raw.strip().casefold() == "forbidden":
            untrained_modifier = None
        elif isinstance(untrained_raw, bool):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.skill_families.{family_id}.untrained must be an integer or 'forbidden'")
        else:
            try:
                untrained_modifier = int(untrained_raw)
            except (TypeError, ValueError) as exc:
                raise SheetSpecError(
                    f"rulepack '{pack_id}': sheet.skill_families.{family_id}.untrained must be an integer or 'forbidden'"
                ) from exc

        for surface in (family_id, *aliases):
            normalized = _normalize_skill_text(surface)
            previous = claimed_aliases.get(normalized)
            if previous is not None and previous != family_id:
                raise SheetSpecError(
                    f"rulepack '{pack_id}': skill family alias {surface!r} is claimed by both {previous!r} and {family_id!r}"
                )
            claimed_aliases[normalized] = family_id

        families[family_id] = SkillFamilySpec(
            base=base,
            aliases=aliases,
            ranks=ranks,
            untrained_modifier=untrained_modifier,
        )
    return families


def parse_sheet_section(pack_id: str, raw: Any) -> SheetSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SheetSpecError(f"rulepack '{pack_id}': 'sheet' must be a mapping")
    unknown = set(raw) - {
        "label", "attr_keys", "skill_keys", "secondary_keys", "field_keys",
        "attributes", "skills", "secondary", "fields", "hit_points",
        "derived_skills", "derived_attrs", "derived_secondary", "check_values",
        "skill_families", "vitals", "resources",
    }
    if unknown:
        raise SheetSpecError(f"rulepack '{pack_id}': sheet has unknown keys {sorted(unknown)}")
    label = str(raw.get("label") or "").strip()
    if not label:
        raise SheetSpecError(f"rulepack '{pack_id}': sheet.label is required")

    hit_points_raw = raw.get("hit_points")
    hit_points: dict[str, int] | None = None
    if hit_points_raw is not None:
        if not isinstance(hit_points_raw, Mapping):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.hit_points must be a mapping")
        hit_points = {
            "current": int(hit_points_raw.get("current", 0)),
            "max": int(hit_points_raw.get("max", 0)),
        }

    vitals_raw = raw.get("vitals") or {}
    if not isinstance(vitals_raw, Mapping):
        raise SheetSpecError(f"rulepack '{pack_id}': sheet.vitals must be a mapping")
    vitals: list[VitalSpec] = []
    for key, spec in vitals_raw.items():
        if not isinstance(spec, Mapping) or not spec.get("max_key"):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.vitals.{key} needs a max_key")
        start = spec.get("start", "full")
        if start != "full" and not (isinstance(start, Mapping) and "expr" in start):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.vitals.{key}.start must be 'full' or an expr")
        vitals.append(VitalSpec(key=str(key), max_key=str(spec["max_key"]), start=start))

    resources_raw = raw.get("resources") or []
    if not isinstance(resources_raw, (list, tuple)) or len(resources_raw) > MAX_RESOURCES:
        raise SheetSpecError(f"rulepack '{pack_id}': sheet.resources must be a short list")
    resources: list[ResourceSpec] = []
    for entry in resources_raw:
        if not isinstance(entry, Mapping) or not entry.get("id") or not entry.get("label"):
            raise SheetSpecError(f"rulepack '{pack_id}': each sheet.resource needs id and label")
        source = str(entry.get("source") or "attributes")
        if source not in ("attributes", "hit_points"):
            raise SheetSpecError(f"rulepack '{pack_id}': sheet.resource source must be attributes|hit_points")
        if source == "attributes" and (not entry.get("value") or not entry.get("max")):
            raise SheetSpecError(f"rulepack '{pack_id}': attribute-backed resources need value and max keys")
        resources.append(
            ResourceSpec(
                id=str(entry["id"]),
                labels=_resource_labels(pack_id, entry["id"], entry["label"]),
                value_key=str(entry.get("value") or ""),
                max_key=str(entry.get("max") or ""),
                source=source,
            )
        )

    return SheetSpec(
        label=label,
        attr_keys=_str_map(pack_id, "sheet.attr_keys", raw.get("attr_keys")),
        skill_keys=_str_map(pack_id, "sheet.skill_keys", raw.get("skill_keys")),
        secondary_keys=_str_map(pack_id, "sheet.secondary_keys", raw.get("secondary_keys")),
        field_keys=_str_map(pack_id, "sheet.field_keys", raw.get("field_keys")),
        attributes=_any_map(pack_id, "sheet.attributes", raw.get("attributes")),
        skills=_any_map(pack_id, "sheet.skills", raw.get("skills")),
        secondary=_any_map(pack_id, "sheet.secondary", raw.get("secondary")),
        fields=_any_map(pack_id, "sheet.fields", raw.get("fields")),
        hit_points=hit_points,
        derived_skills=_str_map(pack_id, "sheet.derived_skills", raw.get("derived_skills")),
        derived_attrs=_str_map(pack_id, "sheet.derived_attrs", raw.get("derived_attrs")),
        derived_secondary=_str_map(pack_id, "sheet.derived_secondary", raw.get("derived_secondary")),
        check_values=_str_map(pack_id, "sheet.check_values", raw.get("check_values")),
        skill_families=_parse_skill_families(pack_id, raw.get("skill_families")),
        vitals=tuple(vitals),
        resources=tuple(resources),
    )


def _int_or(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def resolve_skill_family(pack: Any, name: str) -> tuple[str, str, SkillFamilySpec, str] | None:
    """Resolve ``Family (specialization)`` or ``Family::specialization``.

    Returns ``(canonical_key, family_id, spec, normalized_specialization)``.
    The family by itself never resolves: a specialization is part of the skill's
    identity rather than metadata attached to a shared family score.
    """
    spec = getattr(pack, "sheet_spec", None)
    if spec is None or not spec.skill_families:
        return None

    text = str(name).strip()
    if _SKILL_FAMILY_SEPARATOR in text:
        family_text, specialization_text = text.split(_SKILL_FAMILY_SEPARATOR, 1)
    else:
        match = _SKILL_FAMILY_RE.match(text)
        if match is None:
            return None
        family_text, specialization_text = match.group(1), match.group(2)

    family_norm = _normalize_skill_text(family_text)
    specialization = _normalize_skill_text(specialization_text)
    if not family_norm or not specialization:
        return None

    for family_id, family in spec.skill_families.items():
        surfaces = (family_id, *family.aliases)
        if any(_normalize_skill_text(surface) == family_norm for surface in surfaces):
            canonical = f"{family_id}{_SKILL_FAMILY_SEPARATOR}{specialization}"
            return canonical, family_id, family, specialization
    return None


def canonical_values(sheet: Any, pack: Any) -> dict[str, Any]:
    spec = pack.sheet_spec
    values: dict[str, Any] = {}
    if spec is None:
        values.update(getattr(sheet, "attributes", {}) or {})
        values.update(getattr(sheet, "skills", {}) or {})
        return values

    skill_key_reverse = {key: canonical for canonical, key in spec.skill_keys.items()}
    for key, value in (getattr(sheet, "skills", {}) or {}).items():
        values[skill_key_reverse.get(key, key)] = value
    for canonical, key in spec.secondary_keys.items():
        secondary = getattr(sheet, "secondary_attributes", {}) or {}
        if key in secondary:
            values[canonical] = secondary[key]
    key_to_canonical = spec.key_to_canonical()
    for key, value in (getattr(sheet, "attributes", {}) or {}).items():
        values[key_to_canonical.get(key, key)] = value
    for canonical, field_name in spec.field_keys.items():
        values[canonical] = getattr(sheet, field_name, None)
    return values


def sheet_value(sheet: Any, pack: Any, canonical: str) -> int:
    family_match = resolve_skill_family(pack, canonical)
    if family_match is not None:
        family_key = family_match[0]
        return _int_or((getattr(sheet, "skills", {}) or {}).get(family_key), 0)

    spec = pack.sheet_spec
    if spec is not None:
        attr_key = spec.attr_keys.get(canonical)
        if attr_key and attr_key in sheet.attributes:
            return _int_or(sheet.attributes[attr_key])
        if canonical in ("hp", "hpmax") and spec.hit_points is not None:
            from core.character_manager import get_hit_points

            hp, hp_max = get_hit_points(sheet)
            return hp if canonical == "hp" else hp_max
        secondary_key = spec.secondary_keys.get(canonical)
        if secondary_key and secondary_key in (getattr(sheet, "secondary_attributes", {}) or {}):
            return _int_or(sheet.secondary_attributes[secondary_key])
        skill_key = spec.skill_keys.get(canonical, canonical)
        if skill_key in sheet.skills:
            return _int_or(sheet.skills[skill_key])
        field_name = spec.field_keys.get(canonical)
        if field_name is not None:
            return _int_or(getattr(sheet, field_name, None))

    values = canonical_values(sheet, pack)
    derived = pack.compute_derived(values)
    if canonical in derived:
        return _int_or(derived[canonical])
    if canonical in values:
        return _int_or(values[canonical])
    return _int_or(pack.defaults.get(canonical, 0))


def set_sheet_value(sheet: Any, pack: Any, canonical: str, value: int) -> None:
    family_match = resolve_skill_family(pack, canonical)
    if family_match is not None:
        sheet.skills[family_match[0]] = value
        return

    spec = pack.sheet_spec
    if spec is None:
        sheet.skills[canonical] = value
        return
    attr_key = spec.attr_keys.get(canonical)
    if attr_key:
        sheet.attributes[attr_key] = value
        refresh_sheet(sheet, pack)
        return
    if canonical in ("hp", "hpmax") and spec.hit_points is not None:
        from core.character_manager import set_hit_points

        if canonical == "hp":
            set_hit_points(sheet, current=value, allow_raise_max=True)
        else:
            set_hit_points(sheet, maximum=value)
        return
    secondary_key = spec.secondary_keys.get(canonical)
    if secondary_key:
        sheet.secondary_attributes[secondary_key] = value
        return
    field_name = spec.field_keys.get(canonical)
    if field_name is not None:
        setattr(sheet, field_name, value)
        refresh_sheet(sheet, pack)
        return
    sheet.skills[spec.skill_keys.get(canonical, canonical)] = value


def check_value(sheet: Any, pack: Any, canonical: str) -> int:
    family_match = resolve_skill_family(pack, canonical)
    if family_match is not None:
        family_key, _family_id, family, _specialization = family_match
        rank = _int_or((getattr(sheet, "skills", {}) or {}).get(family_key), 0)
        if rank == 0:
            if family.untrained_modifier is None:
                raise UntrainedSkillError(family_key)
            modifier = family.untrained_modifier
        else:
            if rank not in family.ranks:
                raise ValueError(f"specialized skill rank {rank} is not declared for {family_key}")
            modifier = family.ranks[rank]
        return sheet_value(sheet, pack, family.base) + modifier

    spec = pack.sheet_spec
    if spec is not None:
        canonical = spec.check_values.get(canonical, canonical)
    return sheet_value(sheet, pack, canonical)


def has_check_value(sheet: Any, pack: Any, name: str) -> bool:
    family_match = resolve_skill_family(pack, name)
    if family_match is not None:
        family_key, _family_id, family, _specialization = family_match
        if family.untrained_modifier is not None:
            return True
        rank = _int_or((getattr(sheet, "skills", {}) or {}).get(family_key), 0)
        return rank in family.ranks

    if pack.resolve_skill(name):
        return True
    spec = pack.sheet_spec
    candidates = {name}
    if spec is not None:
        for mapping in (spec.attr_keys, spec.skill_keys, spec.secondary_keys):
            key = mapping.get(name)
            if key:
                candidates.add(key)
    skills = getattr(sheet, "skills", {}) or {}
    attributes = getattr(sheet, "attributes", {}) or {}
    return any(key in skills or key in attributes for key in candidates)


def refresh_sheet(sheet: Any, pack: Any, *, initialize_vitals: bool = False, preserve_trained: bool = True) -> None:
    spec = pack.sheet_spec
    if spec is None:
        return
    values = canonical_values(sheet, pack)
    derived = pack.compute_derived(values)
    namespace = {**values, **{k: v for k, v in derived.items() if k not in values}}

    for attr_key, canonical in spec.derived_attrs.items():
        if canonical in derived:
            sheet.attributes[attr_key] = derived[canonical]
    for secondary_key, canonical in spec.derived_secondary.items():
        if canonical not in derived:
            continue
        if preserve_trained and secondary_key in sheet.secondary_attributes and _int_or(
            sheet.secondary_attributes[secondary_key]
        ) != _int_or(derived[canonical]):
            continue
        sheet.secondary_attributes.pop(secondary_key, None)
    for skill_key, canonical in spec.derived_skills.items():
        if canonical not in derived:
            continue
        if preserve_trained and skill_key in sheet.skills and _int_or(sheet.skills[skill_key]) != _int_or(
            derived[canonical]
        ):
            continue
        sheet.skills.pop(skill_key, None)

    for vital in spec.vitals:
        maximum = _int_or(sheet.attributes.get(vital.max_key), 0)
        if initialize_vitals or vital.key not in sheet.attributes:
            if vital.start == "full":
                start_value = maximum
            else:
                start_expr = dict(vital.start)
                from core.rulepacks import _compile_expr_value

                start_value = _int_or(
                    _compile_expr_value(pack.system, f"vitals.{vital.key}", start_expr, pack.defaults)(
                        {**namespace, **{k: v for k, v in derived.items()}}
                    ),
                    maximum,
                )
            sheet.attributes[vital.key] = max(0, min(maximum, start_value))
        else:
            sheet.attributes[vital.key] = max(0, min(maximum, _int_or(sheet.attributes[vital.key], maximum)))


def wire_resources(sheet: Any, pack: Any, locale: str | None = None) -> list[dict[str, Any]]:
    spec = pack.sheet_spec
    if spec is None:
        return []
    out: list[dict[str, Any]] = []
    for resource in spec.resources:
        if resource.source == "hit_points":
            from core.character_manager import get_hit_points

            value, maximum = get_hit_points(sheet)
        else:
            value = sheet.attributes.get(resource.value_key)
            maximum = sheet.attributes.get(resource.max_key)
            if value is None or maximum is None:
                continue
            value, maximum = _int_or(value), _int_or(maximum)
        out.append({"id": resource.id, "label": resource.label_for(locale), "value": value, "max": maximum})
    return out
