"""Generic, sidecar-driven advancement purchases and XP budgeting.

``core.advancement`` owns pure cost quoting. This module adds the mutation layer
needed by character creation and later campaign advancement without teaching
core any concrete system vocabulary. A pack may optionally declare an
``advancement_purchase.yaml`` beside ``advancement.yaml``. The sidecar defines
how a category moves through its ordered stages and how the next stage is
inferred from the sheet.

State is persisted inside ``CharacterSheet.secondary_attributes`` under one
reserved engine key. That container is already round-tripped by the generic
sheet serializer and ignored by pack canonical-value mapping unless a pack
explicitly names a secondary key, so advancement bookkeeping does not require
system-specific fields on every sheet.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.advancement import AdvancementError, load_advancement_spec, quote_advancement
from core.sheets import resolve_skill_family, set_sheet_value, sheet_value
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__advancement__"


class AdvancementPurchaseError(AdvancementError):
    """An advancement purchase sidecar or requested purchase is invalid."""


@dataclass(frozen=True)
class PurchaseProgression:
    """Mutation rules for one advancement category."""

    stages: tuple[str, ...]
    stage_source: str
    step: int


@dataclass(frozen=True)
class AdvancementPurchaseSpec:
    """Validated purchase rules loaded for one rulepack."""

    progressions: Mapping[str, PurchaseProgression]


@dataclass(frozen=True)
class AdvancementBudget:
    """Current persisted advancement currency state."""

    starting_xp: int
    available_xp: int
    spent_xp: int


@dataclass(frozen=True)
class AdvancementPurchaseQuote:
    """The next legal sequential purchase for one target."""

    category: str
    target: str
    stage: str
    current_value: int
    next_value: int
    aptitude_matches: int
    cost: int
    available_xp: int


@dataclass(frozen=True)
class AdvancementPurchaseResult:
    """One successfully applied purchase."""

    quote: AdvancementPurchaseQuote
    remaining_xp: int
    spent_xp: int


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise AdvancementPurchaseError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise AdvancementPurchaseError("pack system id is not safe for a sidecar path")

    candidates: list[Path] = []
    if data_root is not None:
        candidates.append(Path(data_root) / system / "advancement_purchase.yaml")
    else:
        try:
            import core.rulepacks as rulepacks

            user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
        except Exception:  # pragma: no cover - defensive import boundary
            user_root = None
        if user_root is not None:
            candidates.append(Path(user_root) / "data" / system / "advancement_purchase.yaml")
        candidates.append(_BUILTIN_DATA_ROOT / system / "advancement_purchase.yaml")
    return candidates


def load_advancement_purchase_spec(
    pack: Any, *, data_root: Path | None = None
) -> AdvancementPurchaseSpec | None:
    """Load and validate an optional ``advancement_purchase.yaml`` sidecar."""

    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise AdvancementPurchaseError(f"could not load advancement purchase sidecar {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AdvancementPurchaseError("advancement purchase sidecar root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise AdvancementPurchaseError("unsupported advancement purchase sidecar version")
    unknown = set(raw) - {"version", "progressions"}
    if unknown:
        raise AdvancementPurchaseError(f"advancement purchase sidecar has unknown root keys {sorted(unknown)}")

    progressions_raw = raw.get("progressions")
    if not isinstance(progressions_raw, Mapping) or not progressions_raw:
        raise AdvancementPurchaseError("advancement purchase progressions must be a non-empty mapping")

    progressions: dict[str, PurchaseProgression] = {}
    for category_raw, entry in progressions_raw.items():
        category = str(category_raw).strip()
        if not category or not isinstance(entry, Mapping):
            raise AdvancementPurchaseError("advancement purchase progression entries must be mappings")
        entry_unknown = set(entry) - {"stages", "stage_source", "step"}
        if entry_unknown:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r} has unknown keys {sorted(entry_unknown)}"
            )
        stages_raw = entry.get("stages")
        if (
            not isinstance(stages_raw, (list, tuple))
            or not stages_raw
            or not all(isinstance(stage, str) and stage.strip() for stage in stages_raw)
        ):
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.stages must be a non-empty string list"
            )
        stages = tuple(str(stage).strip() for stage in stages_raw)
        if len({_normalize(stage) for stage in stages}) != len(stages):
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.stages contains duplicates"
            )

        stage_source = str(entry.get("stage_source") or "").strip().casefold()
        if stage_source not in {"history", "value"}:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.stage_source must be history or value"
            )
        step_raw = entry.get("step")
        if isinstance(step_raw, bool):
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.step must be a positive integer"
            )
        try:
            step = int(step_raw)
        except (TypeError, ValueError) as exc:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.step must be a positive integer"
            ) from exc
        if step <= 0:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r}.step must be a positive integer"
            )
        progressions[category] = PurchaseProgression(stages=stages, stage_source=stage_source, step=step)

    advancement = load_advancement_spec(pack, data_root=data_root)
    if advancement is None:
        raise AdvancementPurchaseError("advancement purchase rules require an advancement sidecar")
    cost_categories = {_normalize(name) for name in advancement.costs}
    for category, progression in progressions.items():
        if _normalize(category) not in cost_categories:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r} names an undeclared cost category"
            )
        cost_category = next(name for name in advancement.costs if _normalize(name) == _normalize(category))
        cost_stages = {_normalize(name) for name in advancement.costs[cost_category]}
        unknown_stages = [stage for stage in progression.stages if _normalize(stage) not in cost_stages]
        if unknown_stages:
            raise AdvancementPurchaseError(
                f"advancement purchase progression {category!r} names undeclared stages {unknown_stages}"
            )

    return AdvancementPurchaseSpec(progressions=progressions)


def _state(character: Any, *, create: bool) -> dict[str, Any] | None:
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        if not create:
            return None
        raise AdvancementPurchaseError("character secondary attribute storage must be a mapping")
    raw = secondary.get(_STATE_KEY)
    if raw is None:
        if not create:
            return None
        raw = {"available": 0, "spent": 0, "history": []}
        secondary[_STATE_KEY] = raw
    if not isinstance(raw, dict):
        raise AdvancementPurchaseError("character advancement state must be a mapping")
    return raw


def _read_state(character: Any) -> tuple[int, int, list[dict[str, Any]]] | None:
    raw = _state(character, create=False)
    if raw is None:
        return None
    available_raw = raw.get("available", 0)
    spent_raw = raw.get("spent", 0)
    if isinstance(available_raw, bool) or isinstance(spent_raw, bool):
        raise AdvancementPurchaseError("character advancement XP values must be non-negative integers")
    try:
        available = int(available_raw)
        spent = int(spent_raw)
    except (TypeError, ValueError) as exc:
        raise AdvancementPurchaseError("character advancement XP values must be non-negative integers") from exc
    if available < 0 or spent < 0:
        raise AdvancementPurchaseError("character advancement XP values must be non-negative integers")
    history_raw = raw.get("history", [])
    if not isinstance(history_raw, list) or not all(isinstance(item, Mapping) for item in history_raw):
        raise AdvancementPurchaseError("character advancement history must be a list of mappings")
    history = [dict(item) for item in history_raw]
    return available, spent, history


def initialize_advancement_budget(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
    force: bool = False,
) -> AdvancementBudget | None:
    """Initialize a fresh sheet's starting XP budget when the pack declares one.

    Packs without either advancement sidecar are left untouched. Existing state
    is preserved unless ``force`` is explicitly requested; character creation
    calls this on a brand-new sheet, while later reloads simply read the state.
    """

    advancement = load_advancement_spec(pack, data_root=data_root)
    purchase = load_advancement_purchase_spec(pack, data_root=data_root)
    if advancement is None or purchase is None:
        return None

    existing = _read_state(character)
    if existing is not None and not force:
        return AdvancementBudget(
            starting_xp=advancement.starting_xp,
            available_xp=existing[0],
            spent_xp=existing[1],
        )

    raw = _state(character, create=True)
    assert raw is not None
    raw.clear()
    raw.update({"available": advancement.starting_xp, "spent": 0, "history": []})
    return AdvancementBudget(
        starting_xp=advancement.starting_xp,
        available_xp=advancement.starting_xp,
        spent_xp=0,
    )


def advancement_budget(
    pack: Any, character: Any, *, data_root: Path | None = None
) -> AdvancementBudget | None:
    """Return current XP bookkeeping, initializing a missing fresh state."""

    advancement = load_advancement_spec(pack, data_root=data_root)
    purchase = load_advancement_purchase_spec(pack, data_root=data_root)
    if advancement is None or purchase is None:
        return None
    existing = _read_state(character)
    if existing is None:
        return initialize_advancement_budget(pack, character, data_root=data_root)
    return AdvancementBudget(
        starting_xp=advancement.starting_xp,
        available_xp=existing[0],
        spent_xp=existing[1],
    )


def _resolve_progression(spec: AdvancementPurchaseSpec, category: str) -> tuple[str, PurchaseProgression]:
    wanted = _normalize(category)
    matches = [(name, progression) for name, progression in spec.progressions.items() if _normalize(name) == wanted]
    if not matches:
        raise AdvancementPurchaseError(f"unknown advancement purchase category {category!r}")
    if len(matches) > 1:
        raise AdvancementPurchaseError(f"ambiguous advancement purchase category {category!r}")
    return matches[0]


def _canonical_target(pack: Any, target: str) -> str:
    text = str(target).strip()
    if not text:
        raise AdvancementPurchaseError("advancement purchase target must be non-empty")
    family = resolve_skill_family(pack, text)
    if family is not None:
        return family[0]
    resolved = pack.resolve_skill(text) if hasattr(pack, "resolve_skill") else None
    return resolved or text


def _history_count(history: list[dict[str, Any]], category: str, target: str) -> int:
    category_key = _normalize(category)
    target_key = _normalize(target)
    return sum(
        1
        for item in history
        if _normalize(str(item.get("category", ""))) == category_key
        and _normalize(str(item.get("target", ""))) == target_key
    )


def next_advancement_purchase(
    pack: Any,
    character: Any,
    category: str,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseQuote:
    """Quote the next sequential purchase for ``target`` without mutating it."""

    purchase = load_advancement_purchase_spec(pack, data_root=data_root)
    if purchase is None:
        raise AdvancementPurchaseError(f"rulepack {getattr(pack, 'system', '')!r} has no advancement purchase sidecar")
    category_id, progression = _resolve_progression(purchase, category)
    canonical_target = _canonical_target(pack, target)

    budget = advancement_budget(pack, character, data_root=data_root)
    if budget is None:
        raise AdvancementPurchaseError("rulepack has no advancement budget")
    state = _read_state(character)
    assert state is not None
    _available, _spent, history = state

    current_value = sheet_value(character, pack, canonical_target)
    if progression.stage_source == "history":
        stage_index = _history_count(history, category_id, canonical_target)
    else:
        stage_index = current_value
    if stage_index < 0 or stage_index >= len(progression.stages):
        raise AdvancementPurchaseError(
            f"advancement target {canonical_target!r} has no further {category_id!r} stages"
        )
    stage = progression.stages[stage_index]
    cost_quote = quote_advancement(
        pack,
        character,
        category_id,
        stage,
        target=canonical_target,
        data_root=data_root,
    )
    return AdvancementPurchaseQuote(
        category=cost_quote.category,
        target=canonical_target,
        stage=cost_quote.stage,
        current_value=current_value,
        next_value=current_value + progression.step,
        aptitude_matches=cost_quote.aptitude_matches,
        cost=cost_quote.cost,
        available_xp=budget.available_xp,
    )


def purchase_advancement(
    pack: Any,
    character: Any,
    category: str,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseResult:
    """Atomically buy and apply the next legal sequential advancement stage."""

    quote = next_advancement_purchase(pack, character, category, target, data_root=data_root)
    if quote.cost > quote.available_xp:
        raise AdvancementPurchaseError(
            f"advancement costs {quote.cost} XP but only {quote.available_xp} XP remain"
        )

    snapshot = copy.deepcopy(vars(character))
    try:
        state = _state(character, create=True)
        assert state is not None
        available, spent, history = _read_state(character) or (0, 0, [])

        set_sheet_value(character, pack, quote.target, quote.next_value)
        record = {
            "category": quote.category,
            "target": quote.target,
            "stage": quote.stage,
            "cost": quote.cost,
            "before": quote.current_value,
            "after": quote.next_value,
        }
        history.append(record)
        state["available"] = available - quote.cost
        state["spent"] = spent + quote.cost
        state["history"] = history
        return AdvancementPurchaseResult(
            quote=quote,
            remaining_xp=available - quote.cost,
            spent_xp=spent + quote.cost,
        )
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise
