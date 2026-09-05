"""Generic structured prerequisites for catalog-driven talent advancement.

A rulepack may declare ``talent_requirements.yaml`` beside ``talents.yaml``.
The schema is deliberately small and composable: ``all``/``any`` boolean nodes
plus characteristic/skill/talent/field-membership leaves.  It does not parse
human prose; rulepacks must encode prerequisites explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.advancement_purchase import AdvancementPurchaseError
from core.sheets import resolve_skill_family, sheet_value
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_SPECIALIZATION_RE = re.compile(r"^\s*(.+?)\s*[（(]\s*(.+?)\s*[）)]\s*$")


@dataclass(frozen=True)
class TalentRequirement:
    """One normalized prerequisite node."""

    kind: str
    name: str = ""
    minimum: int = 0
    specialization: str = ""
    any_specialization: bool = False
    field: str = ""
    value: str = ""
    children: tuple["TalentRequirement", ...] = ()


@dataclass(frozen=True)
class TalentRequirementSpec:
    """Validated prerequisite table for one rulepack."""

    talent_field: str
    requirements: Mapping[str, TalentRequirement]
    source: str = ""


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise AdvancementPurchaseError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise AdvancementPurchaseError("pack system id is not safe for a talent requirement path")

    if data_root is not None:
        return [Path(data_root) / system / "talent_requirements.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "talent_requirements.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "talent_requirements.yaml")
    return candidates


def _positive_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool):
        raise AdvancementPurchaseError(f"{where} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AdvancementPurchaseError(f"{where} must be a non-negative integer") from exc
    if number < 0:
        raise AdvancementPurchaseError(f"{where} must be a non-negative integer")
    return number


def _named_minimum(payload: Any, *, where: str, minimum_key: str) -> TalentRequirement:
    if not isinstance(payload, Mapping):
        raise AdvancementPurchaseError(f"{where} must be a mapping")
    unknown = set(payload) - {"name", minimum_key}
    if unknown:
        raise AdvancementPurchaseError(f"{where} has unknown keys {sorted(unknown)}")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise AdvancementPurchaseError(f"{where}.name is required")
    minimum = _positive_int(payload.get(minimum_key), where=f"{where}.{minimum_key}")
    return TalentRequirement(
        kind="stat" if minimum_key == "min" else "skill",
        name=name,
        minimum=minimum,
    )


def _parse_requirement(raw: Any, *, where: str) -> TalentRequirement:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise AdvancementPurchaseError(f"{where} must contain exactly one requirement operator")
    kind, payload = next(iter(raw.items()))
    kind = str(kind).strip()

    if kind in {"all", "any"}:
        if not isinstance(payload, (list, tuple)) or not payload:
            raise AdvancementPurchaseError(f"{where}.{kind} must be a non-empty list")
        return TalentRequirement(
            kind=kind,
            children=tuple(
                _parse_requirement(item, where=f"{where}.{kind}[{index}]")
                for index, item in enumerate(payload)
            ),
        )

    if kind == "stat":
        return _named_minimum(payload, where=f"{where}.stat", minimum_key="min")

    if kind == "skill":
        return _named_minimum(payload, where=f"{where}.skill", minimum_key="min_rank")

    if kind == "talent":
        if not isinstance(payload, Mapping):
            raise AdvancementPurchaseError(f"{where}.talent must be a mapping")
        unknown = set(payload) - {"name", "specialization", "any_specialization"}
        if unknown:
            raise AdvancementPurchaseError(f"{where}.talent has unknown keys {sorted(unknown)}")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise AdvancementPurchaseError(f"{where}.talent.name is required")
        specialization = str(payload.get("specialization") or "").strip()
        any_specialization = payload.get("any_specialization", False)
        if not isinstance(any_specialization, bool):
            raise AdvancementPurchaseError(f"{where}.talent.any_specialization must be boolean")
        if specialization and any_specialization:
            raise AdvancementPurchaseError(
                f"{where}.talent cannot combine specialization with any_specialization"
            )
        return TalentRequirement(
            kind="talent",
            name=name,
            specialization=specialization,
            any_specialization=any_specialization,
        )

    if kind == "field_contains":
        if not isinstance(payload, Mapping):
            raise AdvancementPurchaseError(f"{where}.field_contains must be a mapping")
        unknown = set(payload) - {"field", "value"}
        if unknown:
            raise AdvancementPurchaseError(
                f"{where}.field_contains has unknown keys {sorted(unknown)}"
            )
        field = str(payload.get("field") or "").strip()
        value = str(payload.get("value") or "").strip()
        if not field or not value:
            raise AdvancementPurchaseError(
                f"{where}.field_contains requires non-empty field and value"
            )
        return TalentRequirement(kind="field_contains", field=field, value=value)

    raise AdvancementPurchaseError(f"{where} uses unknown requirement operator {kind!r}")


def load_talent_requirements(
    pack: Any, *, data_root: Path | None = None
) -> TalentRequirementSpec | None:
    """Load and validate an optional ``talent_requirements.yaml`` sidecar."""

    path = next(
        (candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()),
        None,
    )
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise AdvancementPurchaseError(
            f"could not load talent requirement sidecar {path.name!r}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise AdvancementPurchaseError("talent requirement sidecar root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise AdvancementPurchaseError("unsupported talent requirement sidecar version")
    unknown = set(raw) - {"version", "talent_field", "source", "requirements"}
    if unknown:
        raise AdvancementPurchaseError(
            f"talent requirement sidecar has unknown root keys {sorted(unknown)}"
        )

    talent_field = str(raw.get("talent_field") or "Talents").strip()
    if not talent_field:
        raise AdvancementPurchaseError("talent requirement talent_field is required")
    requirements_raw = raw.get("requirements")
    if not isinstance(requirements_raw, Mapping):
        raise AdvancementPurchaseError("talent requirements must be a mapping")

    requirements: dict[str, TalentRequirement] = {}
    claimed: set[str] = set()
    for talent_raw, requirement_raw in requirements_raw.items():
        talent = str(talent_raw).strip()
        if not talent:
            raise AdvancementPurchaseError("talent requirements contain an empty talent name")
        key = _normalize(talent)
        if key in claimed:
            raise AdvancementPurchaseError(
                f"talent requirements contain duplicate talent {talent!r}"
            )
        claimed.add(key)
        requirements[talent] = _parse_requirement(
            requirement_raw, where=f"talent requirement {talent!r}"
        )

    return TalentRequirementSpec(
        talent_field=talent_field,
        requirements=requirements,
        source=str(raw.get("source") or "").strip(),
    )


def _split_talent(value: str) -> tuple[str, str | None]:
    text = str(value).strip()
    if "::" in text:
        name, specialization = text.split("::", 1)
        return name.strip(), specialization.strip() or None
    match = _SPECIALIZATION_RE.match(text)
    if match is not None:
        return match.group(1).strip(), match.group(2).strip()
    return text, None


def _field_values(character: Any, pack: Any, canonical_field: str) -> list[Any]:
    spec = getattr(pack, "sheet_spec", None)
    if spec is None:
        return []
    field_name = spec.field_keys.get(canonical_field)
    if not field_name:
        return []
    values = getattr(character, field_name, None)
    return values if isinstance(values, list) else []


def _owns_talent(
    character: Any,
    pack: Any,
    talent_field: str,
    name: str,
    *,
    specialization: str = "",
    any_specialization: bool = False,
) -> bool:
    wanted_name = _normalize(name)
    wanted_specialization = _normalize(specialization)
    for raw in _field_values(character, pack, talent_field):
        if not isinstance(raw, str):
            continue
        owned_name, owned_specialization = _split_talent(raw)
        if _normalize(owned_name) != wanted_name:
            continue
        if any_specialization:
            return True
        if _normalize(owned_specialization or "") == wanted_specialization:
            return True
    return False


def _canonical_sheet_target(pack: Any, name: str) -> str:
    family = resolve_skill_family(pack, name)
    if family is not None:
        return family[0]
    resolved = pack.resolve_skill(name) if hasattr(pack, "resolve_skill") else None
    return resolved or str(name).strip()


def requirement_met(
    pack: Any,
    character: Any,
    requirement: TalentRequirement,
    *,
    talent_field: str = "Talents",
) -> bool:
    """Evaluate one validated prerequisite node against a character sheet."""

    if requirement.kind == "all":
        return all(
            requirement_met(pack, character, child, talent_field=talent_field)
            for child in requirement.children
        )
    if requirement.kind == "any":
        return any(
            requirement_met(pack, character, child, talent_field=talent_field)
            for child in requirement.children
        )
    if requirement.kind in {"stat", "skill"}:
        canonical = _canonical_sheet_target(pack, requirement.name)
        return sheet_value(character, pack, canonical) >= requirement.minimum
    if requirement.kind == "talent":
        return _owns_talent(
            character,
            pack,
            talent_field,
            requirement.name,
            specialization=requirement.specialization,
            any_specialization=requirement.any_specialization,
        )
    if requirement.kind == "field_contains":
        wanted = _normalize(requirement.value)
        return any(
            isinstance(item, str) and _normalize(item) == wanted
            for item in _field_values(character, pack, requirement.field)
        )
    raise AdvancementPurchaseError(
        f"unsupported normalized talent requirement kind {requirement.kind!r}"
    )


def requirement_for_talent(
    spec: TalentRequirementSpec, target: str
) -> TalentRequirement | None:
    """Resolve a catalog target (including specialization) to its base requirement."""

    name, _specialization = _split_talent(target)
    wanted = _normalize(name)
    matches = [
        requirement
        for talent, requirement in spec.requirements.items()
        if _normalize(talent) == wanted
    ]
    if len(matches) > 1:
        raise AdvancementPurchaseError(f"ambiguous talent requirement target {name!r}")
    return matches[0] if matches else None


def talent_requirements_met(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> bool:
    """Return whether all declared prerequisites for ``target`` are satisfied."""

    spec = load_talent_requirements(pack, data_root=data_root)
    if spec is None:
        return True
    requirement = requirement_for_talent(spec, target)
    if requirement is None:
        return True
    return requirement_met(pack, character, requirement, talent_field=spec.talent_field)


def require_talent_prerequisites(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> None:
    """Raise when a target's declared prerequisites are not currently satisfied."""

    if not talent_requirements_met(pack, character, target, data_root=data_root):
        raise AdvancementPurchaseError(
            f"talent {target!r} prerequisites are not satisfied"
        )
