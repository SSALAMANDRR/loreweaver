"""Manual physical-dice flow for character checks.

This mixin is deliberately transport/UI agnostic. In manual mode a normal
``.check`` computes and freezes the same deterministic inputs the automatic
lane would use, persists one pending roll, and emits a private structured
``roll_request`` frame. A rich client decides how to render that request.

Submission re-enters the existing check implementation with a one-shot dice
provider on a shallow copy of ``Services``. Nothing mutates the deployment-wide
roller and there is no second DH2/check algorithm: the same
``ChecksCommands._cmd_check_generic`` grades, records and emits the result.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from core.character_manager import CharacterSheet
from core.check_roll import favor_modifiers
from core.manual_roll import (
    ROLL_MODE_AUTO,
    ROLL_MODE_MANUAL,
    ManualRollError,
    PendingRoll,
    clear_pending_roll,
    get_roll_mode,
    load_pending_roll,
    manual_roll_detail,
    new_pending_roll,
    parse_manual_dice_expression,
    save_pending_roll,
    set_roll_mode,
)
from core.sheets import check_value, sheet_value
from gateway.commands.checks import (
    _get_rule_variant,
    _pack_for_character,
    _parse_check_args,
    _split_multi,
    _target_value,
)
from gateway.commands.types import CommandCtx, CommandSpec
from gateway.hub import Event


_ROLL_MODE_WORDS = frozenset({"rollmode", "roll_mode"})
_ROLL_SUBMIT_WORD = "__roll_submit"
_FACE_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass(frozen=True)
class _PreparedCheck:
    """Frozen deterministic inputs for one ordinary check request."""

    expression: str
    reason: str
    difficulty_label: str
    target: int | None
    effective_target: int | None
    fingerprint: dict[str, Any]


class _OneShotManualDice:
    """The narrow dice surface ``graded_roll`` needs for one submitted check."""

    def __init__(self, rolled) -> None:
        self.rolled = rolled
        self.used = False

    def roll_for_check(self, resolver, *, params=None, modifiers=None):  # noqa: ANN001, ANN201 - DiceRoller protocol
        if self.used:
            raise ManualRollError("manual roll provider may be consumed only once")  # i18n-exempt: internal invariant
        if params:
            raise ManualRollError("manual check parameters are not supported by this request")  # i18n-exempt: internal invariant
        if modifiers:
            raise ManualRollError("manual named roll modifiers are not supported by this request")  # i18n-exempt: internal invariant
        self.used = True
        return self.rolled


def _hidden_spec(canonical: str, handler, *, private_reply: bool) -> CommandSpec:  # noqa: ANN001
    """Build an internal command spec without putting it in ``.help``."""

    return CommandSpec(
        canonical=canonical,
        handler=handler,
        aliases_en=[canonical],
        aliases_zh=[canonical],
        slash=None,
        help_key="commands.help.roll",
        private_reply=private_reply,
    )


def _difficulty_label(resolver, difficulty_id: str | None, locale: str) -> str:  # noqa: ANN001
    if not difficulty_id:
        return ""
    root = locale.casefold().split("-", 1)[0].split("_", 1)[0]
    for difficulty in resolver.difficulties:
        if difficulty.id != difficulty_id:
            continue
        words = difficulty.prefixes.get(locale) or difficulty.prefixes.get(root)
        if words:
            return str(words[0])
        return difficulty.id
    return difficulty_id


def _request_event(pending: PendingRoll) -> Event:
    return Event.panel(pending.wire(), private=True)


def _cancel_event(request_id: str) -> Event:
    return Event.panel({"type": "roll_cancel", "request_id": request_id}, private=True)


def _parse_submit_args(args: str) -> tuple[str, list[int]]:
    request_id, separator, raw_faces = args.strip().partition(" ")
    if not request_id or not separator or not raw_faces.strip():
        raise ManualRollError("submission needs request id and die faces")  # i18n-exempt: internal parser diagnostic
    faces: list[int] = []
    for token in _FACE_SPLIT_RE.split(raw_faces.strip()):
        if not token:
            continue
        try:
            faces.append(int(token))
        except ValueError as exc:
            raise ManualRollError("manual die faces must be integers") from exc  # i18n-exempt: internal parser diagnostic
    if not faces:
        raise ManualRollError("submission contains no die faces")  # i18n-exempt: internal parser diagnostic
    return request_id, faces


async def _prepare_check(ctx: CommandCtx, character: CharacterSheet, args: str) -> _PreparedCheck:
    """Compute the exact non-random inputs the ordinary check lane will use."""

    pack = await _pack_for_character(ctx, character)
    resolver = pack.resolver
    if resolver is None:
        raise ManualRollError(f"rulepack {pack.system!r} has no check resolver")  # i18n-exempt: internal invariant
    check = resolver.check
    times, rest = _split_multi(args or check.default_skill)
    if times != 1:
        raise ManualRollError("manual mode currently accepts one check at a time")  # i18n-exempt: internal capability gate
    parsed = _parse_check_args(rest, pack, default_name=check.default_skill)
    variant = await _get_rule_variant(ctx)
    modifiers, _applied = favor_modifiers(check, parsed.bonus, parsed.penalty)
    if modifiers:
        # Bonus/penalty/advantage mechanics may change how many dice are thrown or
        # which face is kept. Do not pretend a plain face list models them until
        # the manual substrate explicitly supports that transform.
        raise ManualRollError("named roll modifiers are not supported in manual mode yet")  # i18n-exempt: internal capability gate

    if resolver.target_kind == "dc":
        target_value = parsed.temp_value
        modifier = check_value(character, pack, parsed.canonical)
        if parsed.proficient and check.proficiency:
            modifier += sheet_value(character, pack, check.proficiency)
    else:
        target_value = _target_value(character, pack, parsed.canonical, parsed.temp_value)
        modifier = 0

    expression = resolver.roll
    # Fail before storing anything when the expression cannot yet be represented
    # as physical dice. This is the one syntax gate both request and submission use.
    parse_manual_dice_expression(expression)
    effective = resolver.effective_target(target_value, difficulty=parsed.difficulty)
    display_name = pack.display_name(parsed.canonical, ctx.locale)
    fingerprint = {
        "args": args,
        "system": pack.system,
        "canonical": parsed.canonical,
        "target": target_value,
        "effective_target": effective,
        "modifier": modifier,
        "variant": variant or "",
        "difficulty": parsed.difficulty or "",
        "bonus": parsed.bonus,
        "penalty": parsed.penalty,
        "proficient": parsed.proficient,
        "expression": expression,
    }
    return _PreparedCheck(
        expression=expression,
        reason=display_name,
        difficulty_label=_difficulty_label(resolver, parsed.difficulty, ctx.locale),
        target=target_value,
        effective_target=effective,
        fingerprint=fingerprint,
    )


class ManualRollCommands:
    """Optional physical-dice wrapper around the normal check command."""

    def resolve(self, text: str, locale: str):  # noqa: ANN201 - mirrors CommandRouter.resolve
        """Recognize two internal/manual-control commands without polluting help."""

        stripped = text.strip()
        prefix = next((item for item in self.prefixes if stripped.startswith(item)), "")
        if prefix:
            rest = stripped[len(prefix) :].lstrip()
            token, _separator, args = rest.partition(" ")
            token = token.casefold()
            if token in _ROLL_MODE_WORDS:
                return _hidden_spec("rollmode", self.cmd_roll_mode, private_reply=True), args.strip()
            if token == _ROLL_SUBMIT_WORD:
                # The result of this internal command is normal table content:
                # only the raw command echo is private by the existing turn lane.
                return _hidden_spec(_ROLL_SUBMIT_WORD, self.cmd_manual_roll_submit, private_reply=False), args.strip()
        return super().resolve(text, locale)

    async def cmd_roll_mode(self, ctx: CommandCtx) -> str:
        """Read/set this player's room-local automatic/manual dice preference."""

        arg = ctx.args.strip().casefold()
        if not arg:
            mode = await get_roll_mode(ctx.services.store, ctx.user_id, ctx.chat_key)
            return ctx.i18n.t("commands.manual_roll.mode", mode=mode)
        if arg not in {ROLL_MODE_AUTO, ROLL_MODE_MANUAL}:
            return ctx.fail(ctx.i18n.t("commands.manual_roll.mode_usage"))
        mode = await set_roll_mode(ctx.services.store, ctx.user_id, ctx.chat_key, arg)
        if mode == ROLL_MODE_AUTO:
            pending = await load_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
            if pending is not None:
                await clear_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
                ctx.events.append(_cancel_event(pending.request_id))
        return ctx.i18n.t("commands.manual_roll.mode", mode=mode)

    async def cmd_check(self, ctx: CommandCtx) -> str:
        mode = await get_roll_mode(ctx.services.store, ctx.user_id, ctx.chat_key)
        if mode != ROLL_MODE_MANUAL:
            return await super().cmd_check(ctx)

        existing = await load_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
        if existing is not None:
            ctx.events.append(_request_event(existing))
            return ctx.fail(ctx.i18n.t("commands.manual_roll.pending"))

        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        try:
            prepared = await _prepare_check(ctx, character, ctx.args)
        except ManualRollError:
            expression = ""
            try:
                pack = await _pack_for_character(ctx, character)
                expression = pack.resolver.roll if pack.resolver is not None else ""
            except Exception:
                expression = ""
            return ctx.fail(
                ctx.i18n.t("commands.manual_roll.unsupported", expression=expression or "?")
            )

        pending = new_pending_roll(
            user_id=ctx.user_id,
            kind="check",
            expression=prepared.expression,
            reason=prepared.reason,
            target=prepared.target,
            effective_target=prepared.effective_target,
            difficulty=prepared.difficulty_label,
            context={"fingerprint": prepared.fingerprint},
        )
        await save_pending_roll(ctx.services.store, ctx.chat_key, pending)
        ctx.events.append(_request_event(pending))
        # No fake table result exists yet. Mark the textual half private and empty;
        # the structured request above is the real response a rich client renders.
        return ctx.fail("")

    async def cmd_manual_roll_submit(self, ctx: CommandCtx) -> str:
        """Validate supplied faces and finish the exact frozen check request."""

        pending = await load_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
        if pending is None:
            return ctx.fail(ctx.i18n.t("commands.manual_roll.no_pending"))
        try:
            request_id, faces = _parse_submit_args(ctx.args)
        except ManualRollError:
            return ctx.fail(
                ctx.i18n.t(
                    "commands.manual_roll.bad_faces",
                    expression=pending.expression,
                )
            )
        if request_id != pending.request_id:
            return ctx.fail(ctx.i18n.t("commands.manual_roll.bad_request"))
        if pending.kind != "check":
            return ctx.fail(ctx.i18n.t("commands.manual_roll.bad_request"))

        try:
            rolled = manual_roll_detail(pending.expression, faces)
        except ManualRollError:
            return ctx.fail(
                ctx.i18n.t(
                    "commands.manual_roll.bad_faces",
                    expression=pending.expression,
                )
            )

        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        original_fingerprint = dict((pending.context or {}).get("fingerprint") or {})
        original_args = str(original_fingerprint.get("args") or "")
        try:
            current = await _prepare_check(ctx, character, original_args)
        except ManualRollError:
            current = None
        if current is None or current.fingerprint != original_fingerprint:
            await clear_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
            ctx.events.append(_cancel_event(pending.request_id))
            return ctx.fail(ctx.i18n.t("commands.manual_roll.stale"))

        # Reuse the ordinary check implementation verbatim, but give THIS command
        # invocation a private Services copy whose dice source returns the physical
        # faces exactly once. Other rooms/users keep the real shared DiceRoller.
        manual_dice = _OneShotManualDice(rolled)
        shadow_services = copy.copy(ctx.services)
        shadow_services.dice = manual_dice
        shadow_ctx = copy.copy(ctx)
        shadow_ctx.services = shadow_services
        shadow_ctx.args = original_args
        shadow_ctx.failed = False

        rendered = await super().cmd_check(shadow_ctx)
        if not manual_dice.used:
            raise ManualRollError("manual roll was not consumed by the check lane")  # i18n-exempt: internal invariant
        ctx.failed = shadow_ctx.failed
        await clear_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
        ctx.events.append(_cancel_event(pending.request_id))
        return rendered