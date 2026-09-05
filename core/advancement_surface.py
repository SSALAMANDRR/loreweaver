"""Read-only advancement discovery and safe player-facing purchase helpers.

The low-level purchase modules intentionally expose primitive operations. This
module composes them into the surface used by commands and clients: it never
initializes a missing starting-XP budget, refuses bare specialization families,
and can enumerate concrete next characteristic, skill and catalog-talent buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.advancement import load_advancement_spec
from core.advancement_purchase import (
    AdvancementBudget,
    AdvancementPurchaseError,
    AdvancementPurchaseQuote,
    AdvancementPurchaseResult,
    advancement_budget,
    load_advancement_purchase_spec,
    next_advancement_purchase,
    purchase_advancement,
)
from core.sheets import resolve_skill_family
from core.talent_advancement import (
    available_talent_purchases,
    load_talent_catalog,
    purchase_talent,
    quote_talent_purchase,
)


@dataclass(frozen=True)
class AdvancementSurface:
    """A persisted budget plus the next concrete purchases visible to a client."""

    budget: AdvancementBudget
    purchases: tuple[AdvancementPurchaseQuote, ...]


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("_", " ").split())


def _is_bare_skill_family(pack: Any, target: str) -> bool:
    spec = getattr(pack, "sheet_spec", None)
    if spec is None:
        return False
    wanted = _normalize(target)
    for family_id, family in spec.skill_families.items():
        if any(_normalize(surface) == wanted for surface in (family_id, *family.aliases)):
            return True
    return False


def _require_specialization(pack: Any, target: str) -> None:
    text = str(target).strip()
    if _is_bare_skill_family(pack, text):
        raise AdvancementPurchaseError("specialized skill purchase requires an explicit specialization")
    if "::" in text and resolve_skill_family(pack, text) is None:
        raise AdvancementPurchaseError("specialized skill purchase has an invalid specialization")


def initialized_advancement_budget(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> AdvancementBudget | None:
    """Return an existing XP budget without ever granting starting XP.

    Character creation owns initialization. Read/purchase surfaces deliberately
    treat a missing state as uninitialized so opening a command on a legacy sheet
    cannot mint a fresh starting allowance.
    """

    if load_advancement_spec(pack, data_root=data_root) is None:
        return None
    if load_advancement_purchase_spec(pack, data_root=data_root) is None:
        return None
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict) or "__advancement__" not in secondary:
        return None
    return advancement_budget(pack, character, data_root=data_root)


def safe_next_advancement_purchase(
    pack: Any,
    character: Any,
    category: str,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseQuote:
    """Quote a concrete next purchase without implicitly creating an XP budget."""

    if initialized_advancement_budget(pack, character, data_root=data_root) is None:
        raise AdvancementPurchaseError("character advancement budget is not initialized")
    if _normalize(category) == "talent" and load_talent_catalog(pack, data_root=data_root) is not None:
        return quote_talent_purchase(pack, character, target, data_root=data_root)
    _require_specialization(pack, target)
    return next_advancement_purchase(pack, character, category, target, data_root=data_root)


def safe_purchase_advancement(
    pack: Any,
    character: Any,
    category: str,
    target: str,
    *,
    data_root: Path | None = None,
) -> AdvancementPurchaseResult:
    """Apply one purchase after the same guards used by the player-facing quote."""

    if initialized_advancement_budget(pack, character, data_root=data_root) is None:
        raise AdvancementPurchaseError("character advancement budget is not initialized")
    if _normalize(category) == "talent" and load_talent_catalog(pack, data_root=data_root) is not None:
        return purchase_talent(pack, character, target, data_root=data_root)
    safe_next_advancement_purchase(pack, character, category, target, data_root=data_root)
    return purchase_advancement(pack, character, category, target, data_root=data_root)


def available_advancement_surface(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> AdvancementSurface | None:
    """Return the persisted budget and every concrete next purchase.

    Generic requirement targets are enumerable directly. Bare special-skill
    families are templates rather than purchasable skills, so the list includes
    only specializations already present on the sheet; a player can still enter a
    new explicit specialization directly and have it quoted/purchased safely.

    Talent catalogs enumerate unspecialized talents and finite specialization
    choices. Free-form talent specializations are intentionally omitted from the
    list but remain directly purchasable when the player names one explicitly.
    """

    budget = initialized_advancement_budget(pack, character, data_root=data_root)
    if budget is None:
        return None
    advancement = load_advancement_spec(pack, data_root=data_root)
    purchase_spec = load_advancement_purchase_spec(pack, data_root=data_root)
    assert advancement is not None and purchase_spec is not None

    quotes: list[AdvancementPurchaseQuote] = []
    seen: set[tuple[str, str]] = set()
    sheet_skills = getattr(character, "skills", {}) or {}

    for category in purchase_spec.progressions:
        requirement_category = next(
            (name for name in advancement.requirements if _normalize(name) == _normalize(category)),
            None,
        )
        if requirement_category is None:
            continue

        targets: list[str] = []
        for target in advancement.requirements[requirement_category]:
            if _is_bare_skill_family(pack, target):
                prefix = f"{target}::"
                targets.extend(
                    key for key in sheet_skills
                    if isinstance(key, str) and key.startswith(prefix) and key[len(prefix):].strip()
                )
            else:
                targets.append(target)

        for target in targets:
            key = (_normalize(category), _normalize(target))
            if key in seen:
                continue
            seen.add(key)
            try:
                quotes.append(
                    safe_next_advancement_purchase(
                        pack,
                        character,
                        category,
                        target,
                        data_root=data_root,
                    )
                )
            except AdvancementPurchaseError:
                continue

    for quote in available_talent_purchases(pack, character, data_root=data_root):
        key = (_normalize(quote.category), _normalize(quote.target))
        if key not in seen:
            seen.add(key)
            quotes.append(quote)

    return AdvancementSurface(budget=budget, purchases=tuple(quotes))
