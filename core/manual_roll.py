"""Generic manual-dice input and pending-roll persistence.

The rules engine owns *what* must be rolled and how an already-rolled value is
interpreted; the player/client may own *where the natural faces come from*.
This module is that seam. It converts explicitly supplied natural faces into
the same :class:`core.check_outcome.RollDetail` shape the random roller emits,
and persists one pending request per room participant so a rich client can ask
for physical dice without teaching the rulepack or the UI about one another.

Only deterministic, inspectable dice expressions are accepted for manual
submission here. The first slice covers the ordinary additive/keep forms used
by core character generation and many damage/check formulas (``1d100``,
``2d10+20``, ``3d10kh2+20``, ``3d10kl2+20``). Pools/explosions/fudge and named
roll modifiers stay on the automatic roller until they receive equally explicit
manual semantics; unsupported syntax fails loudly rather than pretending a
player supplied enough information.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.check_outcome import RollDetail
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet


PENDING_ROLL_PREFIX = "pending_roll."
ROLL_MODE_STORE_PREFIX = "manual_roll_mode."
ROLL_MODE_AUTO = "auto"
ROLL_MODE_MANUAL = "manual"
ROLL_MODES = frozenset({ROLL_MODE_AUTO, ROLL_MODE_MANUAL})

# Simple deterministic dice grammar for values a human can physically report:
#   NdM
#   NdM +/- K
#   NdMkhK +/- K
#   NdMklK +/- K
# Whitespace is accepted around the flat modifier only.  This intentionally does
# NOT silently approximate exploding dice, pools, fudge dice, rerolls, or other
# transforms where the exact sequence of natural faces has extra semantics.
_MANUAL_EXPR_RE = re.compile(
    r"^\s*(\d*)d(\d+)(?:(kh|kl)(\d+))?\s*([+-]\s*\d+)?\s*$",
    re.IGNORECASE,
)
_MAX_MANUAL_DICE = 1000
_MAX_MANUAL_SIDES = 100000


class ManualRollError(ValueError):
    """A manual-roll request/result cannot be represented safely."""


@dataclass(frozen=True)
class ManualDiceSpec:
    """The physical dice a supported expression asks the player to throw."""

    expression: str
    count: int
    sides: int
    keep: str = ""
    keep_count: int = 0
    modifier: int = 0

    def wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "expression": self.expression,
            "count": self.count,
            "sides": self.sides,
        }
        if self.keep:
            payload["keep"] = self.keep
            payload["keep_count"] = self.keep_count
        if self.modifier:
            payload["modifier"] = self.modifier
        return payload


@dataclass(frozen=True)
class PendingRoll:
    """One unresolved player-facing roll.

    ``context`` is opaque deterministic continuation data owned by the caller
    that created the request. It never crosses the wire; :meth:`wire` exposes
    only presentation-safe request metadata.
    """

    request_id: str
    user_id: str
    kind: str
    expression: str
    reason: str
    target: int | None = None
    effective_target: int | None = None
    difficulty: str = ""
    context: Mapping[str, Any] | None = None

    def wire(self) -> dict[str, Any]:
        spec = parse_manual_dice_expression(self.expression)
        payload: dict[str, Any] = {
            "type": "roll_request",
            "request_id": self.request_id,
            "kind": self.kind,
            "reason": self.reason,
            **spec.wire(),
        }
        if self.target is not None:
            payload["target"] = self.target
        if self.effective_target is not None:
            payload["effective_target"] = self.effective_target
        if self.difficulty:
            payload["difficulty"] = self.difficulty
        return payload

    def dump(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "request_id": self.request_id,
                "user_id": self.user_id,
                "kind": self.kind,
                "expression": self.expression,
                "reason": self.reason,
                "target": self.target,
                "effective_target": self.effective_target,
                "difficulty": self.difficulty,
                "context": dict(self.context or {}),
            },
            ensure_ascii=False,
        )

    @classmethod
    def load(cls, raw: str) -> "PendingRoll":
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ManualRollError("pending roll state is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ManualRollError("unsupported pending roll state")
        request_id = data.get("request_id")
        user_id = data.get("user_id")
        kind = data.get("kind")
        expression = data.get("expression")
        reason = data.get("reason")
        context = data.get("context") or {}
        if not all(isinstance(value, str) and value for value in (request_id, user_id, kind, expression)):
            raise ManualRollError("pending roll state is missing required fields")
        if not isinstance(reason, str) or not isinstance(context, dict):
            raise ManualRollError("pending roll state has invalid metadata")
        target = _optional_int(data.get("target"), "target")
        effective_target = _optional_int(data.get("effective_target"), "effective_target")
        difficulty = data.get("difficulty") or ""
        if not isinstance(difficulty, str):
            raise ManualRollError("pending roll difficulty must be text")
        # Re-parse here so corrupt/unsupported expressions fail closed as soon as
        # persisted state is read, not only after the player submits a face.
        parse_manual_dice_expression(expression)
        return cls(
            request_id=request_id,
            user_id=user_id,
            kind=kind,
            expression=expression,
            reason=reason,
            target=target,
            effective_target=effective_target,
            difficulty=difficulty,
            context=context,
        )


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ManualRollError(f"pending roll {field} must be numeric")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ManualRollError(f"pending roll {field} must be numeric") from exc


def parse_manual_dice_expression(expression: str) -> ManualDiceSpec:
    """Compile one manual-safe dice expression into a physical-dice spec."""

    text = str(expression or "").strip().lower()
    match = _MANUAL_EXPR_RE.fullmatch(text)
    if match is None:
        raise ManualRollError(f"manual dice do not support expression {expression!r}")
    count = int(match.group(1) or "1")
    sides = int(match.group(2))
    keep = match.group(3) or ""
    keep_count = int(match.group(4) or "0")
    modifier = int((match.group(5) or "0").replace(" ", ""))
    if count < 1 or count > _MAX_MANUAL_DICE:
        raise ManualRollError(f"manual dice count must be 1..{_MAX_MANUAL_DICE}")
    if sides < 2 or sides > _MAX_MANUAL_SIDES:
        raise ManualRollError(f"manual die sides must be 2..{_MAX_MANUAL_SIDES}")
    if keep:
        if keep_count < 1 or keep_count > count:
            raise ManualRollError("manual keep count must be within the rolled dice count")
    elif keep_count:
        raise ManualRollError("manual keep count needs kh/kl")
    return ManualDiceSpec(
        expression=text,
        count=count,
        sides=sides,
        keep=keep,
        keep_count=keep_count,
        modifier=modifier,
    )


def manual_roll_detail(expression: str, faces: Sequence[int]) -> RollDetail:
    """Turn player-supplied natural faces into the normal neutral roll contract.

    The submitted list always contains *every physical die thrown*, including
    dice a ``kh``/``kl`` selector later drops. This preserves enough information
    for future mechanics/records to distinguish e.g. ``[10, 10, 1]`` from a
    bare kept total of 20.
    """

    spec = parse_manual_dice_expression(expression)
    values: list[int] = []
    for raw in faces:
        if isinstance(raw, bool):
            raise ManualRollError("manual die faces must be integers")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ManualRollError("manual die faces must be integers") from exc
        values.append(value)
    if len(values) != spec.count:
        raise ManualRollError(f"expected {spec.count} die face(s), got {len(values)}")
    for value in values:
        if value < 1 or value > spec.sides:
            raise ManualRollError(f"d{spec.sides} face must be between 1 and {spec.sides}")

    kept_indices = set(range(spec.count))
    if spec.keep:
        ordered = sorted(range(spec.count), key=lambda index: (values[index], index))
        selected = ordered[: spec.keep_count] if spec.keep == "kl" else ordered[-spec.keep_count :]
        kept_indices = set(selected)
    kept = tuple(value for index, value in enumerate(values) if index in kept_indices)

    modifiers: dict[str, Any] = {"source": "manual"}
    if spec.modifier:
        modifiers["modifier"] = spec.modifier
    if len(kept) != len(values):
        modifiers["dice_all"] = list(values)
    return RollDetail(
        expression=spec.expression,
        dice=kept,
        total=sum(kept) + spec.modifier,
        modifiers=modifiers,
    )


def new_pending_roll(
    *,
    user_id: str,
    kind: str,
    expression: str,
    reason: str,
    target: int | None = None,
    effective_target: int | None = None,
    difficulty: str = "",
    context: Mapping[str, Any] | None = None,
) -> PendingRoll:
    """Create a validated pending request with a fresh opaque id."""

    parse_manual_dice_expression(expression)
    return PendingRoll(
        request_id=uuid.uuid4().hex,
        user_id=str(user_id),
        kind=str(kind),
        expression=str(expression).strip(),
        reason=str(reason),
        target=target,
        effective_target=effective_target,
        difficulty=str(difficulty or ""),
        context=dict(context or {}),
    )


def _pending_key(user_id: str) -> str:
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:24]
    return f"{PENDING_ROLL_PREFIX}{digest}"


def _mode_key(chat_key: str) -> str:
    digest = hashlib.sha256(str(chat_key).encode("utf-8")).hexdigest()[:24]
    return f"{ROLL_MODE_STORE_PREFIX}{digest}"


async def load_pending_roll(store: Any, chat_key: str, user_id: str) -> PendingRoll | None:
    raw = await store.state_get(chat_key, _pending_key(user_id))
    if not raw:
        return None
    pending = PendingRoll.load(raw)
    if pending.user_id != str(user_id):
        raise ManualRollError("pending roll belongs to a different user")
    return pending


async def save_pending_roll(store: Any, chat_key: str, pending: PendingRoll) -> None:
    await store.state_set(chat_key, _pending_key(pending.user_id), pending.dump())


async def clear_pending_roll(store: Any, chat_key: str, user_id: str) -> None:
    await store.state_delete(chat_key, _pending_key(user_id))


async def get_roll_mode(store: Any, user_id: str, chat_key: str) -> str:
    """Return this user's dice-input preference for this room (auto by default)."""

    try:
        raw = await store.get(user_key=str(user_id), store_key=_mode_key(chat_key))
    except Exception:
        return ROLL_MODE_AUTO
    return raw if raw in ROLL_MODES else ROLL_MODE_AUTO


async def set_roll_mode(store: Any, user_id: str, chat_key: str, mode: str) -> str:
    normalized = str(mode).strip().casefold()
    if normalized not in ROLL_MODES:
        raise ManualRollError(f"unknown roll mode: {mode}")
    await store.set(user_key=str(user_id), store_key=_mode_key(chat_key), value=normalized)
    return normalized


# Pending physical dice are an in-progress story interaction, not room settings.
# A story reset cancels them; a room export/import carries them like the rest of
# room_state so restoring a checkpoint cannot leave continuation metadata detached
# from the state it was created against.
ROOM_FACETS = (
    RoomStateFacet(
        name="pending_manual_rolls",
        owner="core.manual_roll",
        reset_scope="story",
        state_prefixes=frozenset({PENDING_ROLL_PREFIX}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
