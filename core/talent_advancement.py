"""Generic catalog-driven talent advancement purchases.

A rulepack may declare ``talents.yaml`` beside its other advancement sidecars.
Catalog entries are quoted and purchased only after their structured prerequisite
sidecar has been satisfied. Specializations remain explicit player choices;
free-form specializations are accepted only when the catalog says so. Catalogs
may also declare spelling/legacy aliases that normalize to canonical choices.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.advancement import quote_advancement
from core.advancement_purchase import (
    AdvancementPurchaseError,
    AdvancementPurchaseQuote,
    AdvancementPurchaseResult,
    advancement_budget,
)
from core.talent_requirements import require_talent_prerequisites
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__advancement__"
_SPECIALIZATION_RE = re.compile(r"^\s*(.+?)\s*[（(]\s*(.+?)\s*[）)]\s*$")


@dataclass(frozen=True)
class TalentSpecializations:
    """Specialization policy for one catalog talent."""

    required: bool
    free: bool
    choices: tuple[str, ...]
    aliases: Mapping[str, str]
    aptitude_overrides: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class TalentEntry:
    """One XP-purchasable talent with resolved purchase metadata."""

    name: str
    tier: int
    aptitudes: tuple[str, ...]
    specializations: TalentSpecializations | None = None
    source: str = ""


@dataclass(frozen=True)
class TalentCatalog:
    """Validated talent purchase catalog for one rulepack."""

    field: str
    talents: Mapping[str, TalentEntry]
    source: str = ""


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise AdvancementPurchaseError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise AdvancementPurchaseError("pack system id is not safe for a talent catalog path")

    candidates: list[Path] = []
    if data_root is not None:
        candidates.append(Path(data_root) / system / "talents.yaml")
    else:
        try:
            import core.rulepacks as rulepacks

            user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
        except Exception:  # pragma: no cover - defensive import boundary
            user_root = None
        if user_root is not None:
            candidates.append(Path(user_root) / "data" / system / "talents.yaml")
        candidates.append(_BUILTIN_DATA_ROOT / system / "talents.yaml")
    return candidates


def _string_list(value: Any, *, where: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        kind = "possibly empty" if allow_empty else "non-empty"
        raise AdvancementPurchaseError(f"{where} must be a {kind} string list")
    items = tuple(str(item).strip() for item in value if isinstance(item, str) and str(item).strip())
    if len(items) != len(value):
        raise AdvancementPurchaseError(f"{where} must contain only non-empty strings")
    if len({_normalize(item) for item in items}) != len(items):
        raise AdvancementPurchaseError(f"{where} contains duplicate values")
    return items


def load_talent_catalog(pack: Any, *, data_root: Path | None = None) -> TalentCatalog | None:
    """Load and validate an optional ``talents.yaml`` purchase catalog."""

    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise AdvancementPurchaseError(f"could not load talent catalog {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AdvancementPurchaseError("talent catalog root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise AdvancementPurchaseError("unsupported talent catalog version")
    unknown = set(raw) - {"version", "field", "source", "talents"}
    if unknown:
        raise AdvancementPurchaseError(f"talent catalog has unknown root keys {sorted(unknown)}")

    field = str(raw.get("field") or "").strip()
    if not field:
        raise AdvancementPurchaseError("talent catalog field is required")
    source = str(raw.get("source") or "").strip()
    talents_raw = raw.get("talents")
    if not isinstance(talents_raw, Mapping) or not talents_raw:
        raise AdvancementPurchaseError("talent catalog talents must be a non-empty mapping")

    talents: dict[str, TalentEntry] = {}
    claimed: set[str] = set()
    for name_raw, entry_raw in talents_raw.items():
        name = str(name_raw).strip()
        if not name or not isinstance(entry_raw, Mapping):
            raise AdvancementPurchaseError("talent catalog entries must be named mappings")
        name_key = _normalize(name)
        if name_key in claimed:
            raise AdvancementPurchaseError(f"talent catalog contains duplicate talent {name!r}")
        claimed.add(name_key)

        unknown_entry = set(entry_raw) - {"tier", "aptitudes", "specializations", "source"}
        if unknown_entry:
            raise AdvancementPurchaseError(f"talent {name!r} has unknown keys {sorted(unknown_entry)}")
        tier_raw = entry_raw.get("tier")
        if isinstance(tier_raw, bool):
            raise AdvancementPurchaseError(f"talent {name!r}.tier must be 1, 2 or 3")
        try:
            tier = int(tier_raw)
        except (TypeError, ValueError) as exc:
            raise AdvancementPurchaseError(f"talent {name!r}.tier must be 1, 2 or 3") from exc
        if tier not in {1, 2, 3}:
            raise AdvancementPurchaseError(f"talent {name!r}.tier must be 1, 2 or 3")

        aptitudes = _string_list(entry_raw.get("aptitudes"), where=f"talent {name!r}.aptitudes")
        if len(aptitudes) != 2:
            raise AdvancementPurchaseError(f"talent {name!r}.aptitudes must contain exactly two values")

        specialization_spec: TalentSpecializations | None = None
        special_raw = entry_raw.get("specializations")
        if special_raw is not None:
            if not isinstance(special_raw, Mapping):
                raise AdvancementPurchaseError(f"talent {name!r}.specializations must be a mapping")
            unknown_special = set(special_raw) - {
                "required",
                "free",
                "choices",
                "aliases",
                "aptitude_overrides",
            }
            if unknown_special:
                raise AdvancementPurchaseError(
                    f"talent {name!r}.specializations has unknown keys {sorted(unknown_special)}"
                )
            required_raw = special_raw.get("required", True)
            free_raw = special_raw.get("free", False)
            if not isinstance(required_raw, bool) or not isinstance(free_raw, bool):
                raise AdvancementPurchaseError(
                    f"talent {name!r}.specializations required/free flags must be booleans"
                )
            required = required_raw
            free = free_raw
            choices = _string_list(
                special_raw.get("choices") or [],
                where=f"talent {name!r}.specializations.choices",
                allow_empty=True,
            )
            if required and not free and not choices:
                raise AdvancementPurchaseError(
                    f"talent {name!r} requires either specialization choices or free=true"
                )
            choice_map = {_normalize(choice): choice for choice in choices}

            aliases_raw = special_raw.get("aliases") or {}
            if not isinstance(aliases_raw, Mapping):
                raise AdvancementPurchaseError(
                    f"talent {name!r}.specializations.aliases must be a mapping"
                )
            aliases: dict[str, str] = {}
            for alias_raw, target_raw in aliases_raw.items():
                alias = str(alias_raw).strip()
                target = str(target_raw).strip()
                if not alias or not target:
                    raise AdvancementPurchaseError(
                        f"talent {name!r}.specializations.aliases must map non-empty strings"
                    )
                alias_key = _normalize(alias)
                if alias_key in aliases:
                    raise AdvancementPurchaseError(
                        f"talent {name!r}.specializations.aliases contains duplicate alias {alias!r}"
                    )
                if choices:
                    canonical = choice_map.get(_normalize(target))
                    if canonical is None:
                        raise AdvancementPurchaseError(
                            f"talent {name!r} specialization alias {alias!r} targets undeclared choice {target!r}"
                        )
                else:
                    canonical = target
                aliases[alias_key] = canonical

            overrides_raw = special_raw.get("aptitude_overrides") or {}
            if not isinstance(overrides_raw, Mapping):
                raise AdvancementPurchaseError(
                    f"talent {name!r}.specializations.aptitude_overrides must be a mapping"
                )
            overrides: dict[str, tuple[str, ...]] = {}
            for specialization_raw, aptitude_raw in overrides_raw.items():
                specialization = str(specialization_raw).strip()
                if not specialization:
                    raise AdvancementPurchaseError(
                        f"talent {name!r} has an empty specialization aptitude override"
                    )
                if choices and _normalize(specialization) not in choice_map:
                    raise AdvancementPurchaseError(
                        f"talent {name!r} aptitude override names undeclared specialization {specialization!r}"
                    )
                override = _string_list(
                    aptitude_raw,
                    where=f"talent {name!r} specialization {specialization!r} aptitudes",
                )
                if len(override) != 2:
                    raise AdvancementPurchaseError(
                        f"talent {name!r} specialization {specialization!r} needs exactly two aptitudes"
                    )
                overrides[_normalize(specialization)] = override
            specialization_spec = TalentSpecializations(
                required=required,
                free=free,
                choices=choices,
                aliases=aliases,
                aptitude_overrides=overrides,
            )

        talents[name] = TalentEntry(
            name=name,
            tier=tier,
            aptitudes=aptitudes,
            specializations=specialization_spec,
            source=str(entry_raw.get("source") or "").strip(),
        )

    return TalentCatalog(field=field, talents=talents, source=source)


def _split_target(target: str) -> tuple[str, str | None]:
    text = str(target).strip()
    if not text:
        raise AdvancementPurchaseError("talent target must be non-empty")
    if "::" in text:
        name, specialization = text.split("::", 1)
        name = name.strip()
        specialization = specialization.strip()
        if not name or not specialization:
            raise AdvancementPurchaseError("talent specialization must be non-empty")
        return name, specialization
    match = _SPECIALIZATION_RE.match(text)
    if match is not None:
        return match.group(1).strip(), match.group(2).strip()
    return text, None


def _canonicalize_specialization(
    entry: TalentEntry, specialization: str | None, *, strict: bool = True
) -> str | None:
    policy = entry.specializations
    if policy is None:
        if specialization is not None and strict:
            raise AdvancementPurchaseError(f"talent {entry.name!r} does not take a specialization")
        return specialization
    if specialization is None:
        return None

    alias_target = policy.aliases.get(_normalize(specialization))
    if alias_target is not None:
        specialization = alias_target

    if policy.choices:
        choice = next((item for item in policy.choices if _normalize(item) == _normalize(specialization)), None)
        if choice is not None:
            return choice
        if strict:
            raise AdvancementPurchaseError(
                f"talent {entry.name!r} does not allow specialization {specialization!r}"
            )
        return specialization
    if policy.free:
        return specialization
    if strict:
        raise AdvancementPurchaseError(f"talent {entry.name!r} does not allow free specializations")
    return specialization


def _resolve_entry(catalog: TalentCatalog, target: str) -> tuple[TalentEntry, str | None, tuple[str, ...]]:
    name_text, specialization = _split_target(target)
    wanted = _normalize(name_text)
    matches = [entry for entry in catalog.talents.values() if _normalize(entry.name) == wanted]
    if not matches:
        raise AdvancementPurchaseError(f"unknown purchasable talent {name_text!r}")
    if len(matches) > 1:
        raise AdvancementPurchaseError(f"ambiguous purchasable talent {name_text!r}")
    entry = matches[0]
    policy = entry.specializations

    if policy is None:
        if specialization is not None:
            raise AdvancementPurchaseError(f"talent {entry.name!r} does not take a specialization")
        return entry, None, entry.aptitudes

    if policy.required and specialization is None:
        raise AdvancementPurchaseError(f"talent {entry.name!r} requires a specialization")
    specialization = _canonicalize_specialization(entry, specialization)
    if specialization is None:
        return entry, None, entry.aptitudes

    aptitudes = policy.aptitude_overrides.get(_normalize(specialization), entry.aptitudes)
    return entry, specialization, aptitudes


def _talent_field(character: Any, pack: Any, catalog: TalentCatalog) -> list[Any]:
    spec = getattr(pack, "sheet_spec", None)
    if spec is None:
        raise AdvancementPurchaseError("talent catalog requires a pack sheet spec")
    field_name = spec.field_keys.get(catalog.field)
    if not field_name:
        raise AdvancementPurchaseError(
            f"talent catalog field {catalog.field!r} is not mapped by the pack sheet"
        )
    values = getattr(character, field_name, None)
    if not isinstance(values, list):
        raise AdvancementPurchaseError(f"character talent field {field_name!r} must be a list")
    return values


def _storage_text(name: str, specialization: str | None) -> str:
    return name if specialization is None else f"{name} ({specialization})"


def _canonical_target(name: str, specialization: str | None) -> str:
    return name if specialization is None else f"{name}::{specialization}"


def _owns_talent(values: list[Any], entry: TalentEntry, specialization: str | None) -> bool:
    wanted_name = _normalize(entry.name)
    wanted_specialization = _normalize(specialization or "")
    for raw in values:
        if not isinstance(raw, str):
            continue
        try:
            owned_name, owned_specialization = _split_target(raw)
        except AdvancementPurchaseError:
            continue
        if _normalize(owned_name) != wanted_name:
            continue
        canonical_owned = _canonicalize_specialization(entry, owned_specialization, strict=False)
        if _normalize(canonical_owned or "") == wanted_specialization:
            return True
    return False


def _existing_budget(pack: Any, character: Any, *, data_root: Path | None = None):
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict) or _STATE_KEY not in secondary:
        raise AdvancementPurchaseError("character advancement budget is not initialized")
    budget = advancement_budget(pack, character, data_root=data_root)
    if budget is None:
        raise AdvancementPurchaseError("rulepack has no advancement budget")
    return budget


def quote_talent_purchase(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseQuote:
    """Quote one legal catalog talent without mutating the sheet or minting XP."""

    catalog = load_talent_catalog(pack, data_root=data_root)
    if catalog is None:
        raise AdvancementPurchaseError(f"rulepack {getattr(pack, 'system', '')!r} has no talent catalog")
    budget = _existing_budget(pack, character, data_root=data_root)
    values = _talent_field(character, pack, catalog)
    entry, specialization, aptitudes = _resolve_entry(catalog, target)
    if _owns_talent(values, entry, specialization):
        raise AdvancementPurchaseError(f"talent {_storage_text(entry.name, specialization)!r} is already owned")

    canonical_target = _canonical_target(entry.name, specialization)
    require_talent_prerequisites(pack, character, canonical_target, data_root=data_root)

    cost_quote = quote_advancement(
        pack,
        character,
        "talent",
        f"tier_{entry.tier}",
        required_aptitudes=aptitudes,
        data_root=data_root,
    )
    return AdvancementPurchaseQuote(
        category="talent",
        target=canonical_target,
        stage=cost_quote.stage,
        current_value=0,
        next_value=1,
        aptitude_matches=cost_quote.aptitude_matches,
        cost=cost_quote.cost,
        available_xp=budget.available_xp,
    )


def purchase_talent(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseResult:
    """Atomically append one legal catalog talent and spend its quoted XP."""

    quote = quote_talent_purchase(pack, character, target, data_root=data_root)
    if quote.cost > quote.available_xp:
        raise AdvancementPurchaseError(
            f"talent costs {quote.cost} XP but only {quote.available_xp} XP remain"
        )

    catalog = load_talent_catalog(pack, data_root=data_root)
    assert catalog is not None
    entry, specialization, _aptitudes = _resolve_entry(catalog, target)

    snapshot = copy.deepcopy(vars(character))
    try:
        values = _talent_field(character, pack, catalog)
        values.append(_storage_text(entry.name, specialization))

        secondary = getattr(character, "secondary_attributes", None)
        if not isinstance(secondary, dict):
            raise AdvancementPurchaseError("character secondary attribute storage must be a mapping")
        state = secondary.get(_STATE_KEY)
        if not isinstance(state, dict):
            raise AdvancementPurchaseError("character advancement state must be a mapping")
        available_raw = state.get("available", 0)
        spent_raw = state.get("spent", 0)
        if isinstance(available_raw, bool) or isinstance(spent_raw, bool):
            raise AdvancementPurchaseError("character advancement XP values must be integers")
        available = int(available_raw)
        spent = int(spent_raw)
        history_raw = state.get("history", [])
        if not isinstance(history_raw, list):
            raise AdvancementPurchaseError("character advancement history must be a list")
        history = list(history_raw)
        history.append(
            {
                "category": quote.category,
                "target": quote.target,
                "stage": quote.stage,
                "cost": quote.cost,
                "before": 0,
                "after": 1,
            }
        )
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


def available_talent_purchases(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> tuple[AdvancementPurchaseQuote, ...]:
    """Enumerate concrete legal talent purchases the catalog exposes."""

    catalog = load_talent_catalog(pack, data_root=data_root)
    if catalog is None:
        return ()
    _existing_budget(pack, character, data_root=data_root)

    targets: list[str] = []
    for entry in catalog.talents.values():
        policy = entry.specializations
        if policy is None or not policy.required:
            targets.append(entry.name)
        elif policy.choices:
            targets.extend(f"{entry.name}::{choice}" for choice in policy.choices)
        # Free-form specializations cannot be enumerated honestly; the player may
        # still quote/buy one explicitly by typing it.

    quotes: list[AdvancementPurchaseQuote] = []
    for target in targets:
        try:
            quotes.append(quote_talent_purchase(pack, character, target, data_root=data_root))
        except AdvancementPurchaseError:
            continue
    return tuple(quotes)
