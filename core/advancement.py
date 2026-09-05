"""Generic, sidecar-driven advancement cost calculation.

Advancement rules are progression data rather than command-routing logic. Systems
may therefore declare an ``advancement.yaml`` beside their other rulepack data
without teaching core about a concrete game id. The sidecar owns the starting XP
budget, the sheet field that stores aptitudes (or analogous affinities), and the
cost matrix for each advancement category/stage.

This module deliberately only QUOTES costs. Applying a characteristic/skill/
talent purchase is a separate mutation step because the target being advanced is
system data too; keeping quoting pure makes it usable by creation, UI previews,
and later purchase validation without half-mutating a character on failure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"


class AdvancementError(ValueError):
    """An advancement sidecar or requested advancement is invalid."""


@dataclass(frozen=True)
class AdvancementSpec:
    """Validated advancement data loaded for one rulepack."""

    starting_xp: int
    aptitude_field: str
    base_aptitudes: tuple[str, ...]
    costs: Mapping[str, Mapping[str, Mapping[int, int]]]
    requirements: Mapping[str, Mapping[str, tuple[str, ...]]]


@dataclass(frozen=True)
class AdvancementQuote:
    """Pure cost quote for one requested advancement stage."""

    category: str
    stage: str
    aptitude_matches: int
    cost: int


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise AdvancementError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise AdvancementError("pack system id is not safe for a sidecar path")

    candidates: list[Path] = []
    if data_root is not None:
        candidates.append(Path(data_root) / system / "advancement.yaml")
    else:
        try:
            import core.rulepacks as rulepacks

            user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
        except Exception:  # pragma: no cover - defensive import boundary
            user_root = None
        if user_root is not None:
            candidates.append(Path(user_root) / "data" / system / "advancement.yaml")
        candidates.append(_BUILTIN_DATA_ROOT / system / "advancement.yaml")
    return candidates


def _parse_costs(raw: Any) -> dict[str, dict[str, dict[int, int]]]:
    if not isinstance(raw, Mapping) or not raw:
        raise AdvancementError("advancement costs must be a non-empty mapping")

    parsed: dict[str, dict[str, dict[int, int]]] = {}
    for category_raw, stages_raw in raw.items():
        category = str(category_raw).strip()
        if not category or not isinstance(stages_raw, Mapping) or not stages_raw:
            raise AdvancementError("advancement cost categories must contain stage mappings")
        stages: dict[str, dict[int, int]] = {}
        for stage_raw, row_raw in stages_raw.items():
            stage = str(stage_raw).strip()
            if not stage or not isinstance(row_raw, Mapping) or not row_raw:
                raise AdvancementError(f"advancement costs.{category} stages must contain match-cost mappings")
            row: dict[int, int] = {}
            for matches_raw, cost_raw in row_raw.items():
                try:
                    matches = int(matches_raw)
                    cost = int(cost_raw)
                except (TypeError, ValueError) as exc:
                    raise AdvancementError(
                        f"advancement costs.{category}.{stage} entries must be integer match-count -> cost pairs"
                    ) from exc
                if isinstance(matches_raw, bool) or isinstance(cost_raw, bool) or matches < 0 or cost < 0:
                    raise AdvancementError(
                        f"advancement costs.{category}.{stage} entries must use non-negative integers"
                    )
                if matches in row:
                    raise AdvancementError(
                        f"advancement costs.{category}.{stage} duplicates match count {matches}"
                    )
                row[matches] = cost
            stages[stage] = row
        parsed[category] = stages
    return parsed


def load_advancement_spec(pack: Any, *, data_root: Path | None = None) -> AdvancementSpec | None:
    """Load and validate a pack's optional ``advancement.yaml`` sidecar."""

    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None

    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise AdvancementError(f"could not load advancement sidecar {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AdvancementError(f"advancement sidecar {path.name!r} root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise AdvancementError(f"unsupported advancement sidecar version in {path.name!r}")
    unknown = set(raw) - {"version", "starting_xp", "aptitude_field", "base_aptitudes", "costs", "requirements"}
    if unknown:
        raise AdvancementError(f"advancement sidecar {path.name!r} has unknown root keys {sorted(unknown)}")

    try:
        starting_xp = int(raw.get("starting_xp", 0))
    except (TypeError, ValueError) as exc:
        raise AdvancementError("advancement starting_xp must be a non-negative integer") from exc
    if isinstance(raw.get("starting_xp", 0), bool) or starting_xp < 0:
        raise AdvancementError("advancement starting_xp must be a non-negative integer")

    aptitude_field = str(raw.get("aptitude_field") or "").strip()
    if not aptitude_field:
        raise AdvancementError("advancement aptitude_field must name a declared sheet field")

    base_aptitudes_raw = raw.get("base_aptitudes") or []
    if (
        not isinstance(base_aptitudes_raw, (list, tuple))
        or not all(isinstance(item, str) and item.strip() for item in base_aptitudes_raw)
    ):
        raise AdvancementError("advancement base_aptitudes must be a string list")
    base_aptitudes = tuple(str(item).strip() for item in base_aptitudes_raw)

    requirements_raw = raw.get("requirements") or {}
    if not isinstance(requirements_raw, Mapping):
        raise AdvancementError("advancement requirements must be a mapping")
    requirements: dict[str, dict[str, tuple[str, ...]]] = {}
    for category_raw, targets_raw in requirements_raw.items():
        category = str(category_raw).strip()
        if not category or not isinstance(targets_raw, Mapping):
            raise AdvancementError("advancement requirement categories must contain target mappings")
        targets: dict[str, tuple[str, ...]] = {}
        for target_raw, aptitudes_raw in targets_raw.items():
            target = str(target_raw).strip()
            if (
                not target
                or not isinstance(aptitudes_raw, (list, tuple))
                or not aptitudes_raw
                or not all(isinstance(item, str) and item.strip() for item in aptitudes_raw)
            ):
                raise AdvancementError(
                    f"advancement requirements.{category} targets must contain non-empty aptitude lists"
                )
            targets[target] = tuple(str(item).strip() for item in aptitudes_raw)
        requirements[category] = targets

    return AdvancementSpec(
        starting_xp=starting_xp,
        aptitude_field=aptitude_field,
        base_aptitudes=base_aptitudes,
        costs=_parse_costs(raw.get("costs")),
        requirements=requirements,
    )


def count_matching_aptitudes(owned: Iterable[str], required: Iterable[str]) -> int:
    """Count unique normalized aptitude names shared by ``owned`` and ``required``."""

    def _names(values: Iterable[str], label: str) -> set[str]:
        if isinstance(values, (str, bytes)):
            raise AdvancementError(f"{label} aptitudes must be an iterable of names, not a string")
        names: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise AdvancementError(f"{label} aptitudes must contain non-empty strings")
            names.add(_normalize(value))
        return names

    return len(_names(owned, "owned").intersection(_names(required, "required")))


def _resolve_table_key(table: Mapping[str, Any], requested: str, where: str) -> str:
    wanted = _normalize(requested)
    if not wanted:
        raise AdvancementError(f"{where} must be non-empty")
    matches = [key for key in table if _normalize(key) == wanted]
    if not matches:
        raise AdvancementError(f"unknown {where} {requested!r}")
    if len(matches) > 1:
        raise AdvancementError(f"ambiguous {where} {requested!r}")
    return matches[0]


def _character_aptitudes(pack: Any, character: Any, canonical_field: str) -> Iterable[str]:
    sheet_spec = getattr(pack, "sheet_spec", None)
    if sheet_spec is None:
        raise AdvancementError("pack has no sheet spec for advancement aptitudes")
    field_name = sheet_spec.field_keys.get(canonical_field)
    if field_name is None:
        raise AdvancementError(f"advancement aptitude field {canonical_field!r} is not declared by the sheet")
    values = getattr(character, field_name, None)
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)):
        raise AdvancementError(f"sheet field {canonical_field!r} must be list-like")
    return values


def advancement_requirements(spec: AdvancementSpec, category: str, target: str) -> tuple[str, ...]:
    """Resolve declared aptitude requirements for a canonical advancement target.

    Specialized skills use the generic ``Family::specialization`` identity. If a
    sidecar declares the family once, every specialization inherits that pair.
    """

    category_id = _resolve_table_key(spec.requirements, category, "advancement requirement category")
    targets = spec.requirements[category_id]
    try:
        target_id = _resolve_table_key(targets, target, "advancement target")
    except AdvancementError:
        family, separator, _specialization = str(target).partition("::")
        if not separator:
            raise
        target_id = _resolve_table_key(targets, family, "advancement target")
    return targets[target_id]


def quote_advancement(
    pack: Any,
    character: Any,
    category: str,
    stage: str,
    required_aptitudes: Iterable[str] | None = None,
    *,
    target: str | None = None,
    data_root: Path | None = None,
) -> AdvancementQuote:
    """Return the declared XP cost for one advancement without mutating the sheet."""

    spec = load_advancement_spec(pack, data_root=data_root)
    if spec is None:
        raise AdvancementError(f"rulepack {getattr(pack, 'system', '')!r} has no advancement sidecar")

    category_id = _resolve_table_key(spec.costs, category, "advancement category")
    stages = spec.costs[category_id]
    stage_id = _resolve_table_key(stages, stage, "advancement stage")
    if (required_aptitudes is None) == (target is None):
        raise AdvancementError("quote needs exactly one of required_aptitudes or target")
    if target is not None:
        required_aptitudes = advancement_requirements(spec, category_id, target)
    assert required_aptitudes is not None
    owned = (*_character_aptitudes(pack, character, spec.aptitude_field), *spec.base_aptitudes)
    matches = count_matching_aptitudes(owned, required_aptitudes)
    row = stages[stage_id]
    if matches not in row:
        raise AdvancementError(
            f"advancement {category_id}.{stage_id} has no cost for {matches} matching aptitudes"
        )
    return AdvancementQuote(
        category=category_id,
        stage=stage_id,
        aptitude_matches=matches,
        cost=row[matches],
    )
