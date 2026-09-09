"""Hidden rich-client mutation lane for structured character creation.

The public `.create`, pack make-character word, and `.advance` handlers remain the
single mutation authority for deterministic creation mechanics. Rich clients invoke
those same handlers without putting implementation commands and success prose into the
narrative log. Pack-declared narrative character context is deliberately non-mechanical;
its hidden action validates through ``core.character_context`` and persists the same
sheet state directly.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from urllib.parse import unquote

from core.character_context import CharacterContextError, set_character_context
from core.character_manager import has_character
from core.creation_finalization import creation_finalization_status
from core.finalization_surface import finalization_surface
from core.rulepacks import load_rulepack, own_make_char_word
from gateway.commands.types import CommandCtx, CommandSpec

_CREATION_ACTION_WORD = "__creation_action"
_ACTIONS = frozenset({"start", "create", "advance", "context", "finalize"})


def _hidden_spec(handler) -> CommandSpec:  # noqa: ANN001
    return CommandSpec(
        canonical=_CREATION_ACTION_WORD,
        handler=handler,
        aliases_en=[_CREATION_ACTION_WORD],
        aliases_zh=[_CREATION_ACTION_WORD],
        slash=None,
        help_key="commands.help.create",
        private_reply=True,
    )


def _start_parts(raw: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in raw.split("|", 2)]
    if not parts or not parts[0]:
        return None
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _context_payload(raw: str) -> tuple[bool, Mapping[str, object]] | None:
    text = raw.strip()
    if text.casefold() == "skip":
        return True, {}
    verb, separator, encoded = text.partition(" ")
    if verb.casefold() != "set" or not separator or not encoded.strip():
        return None
    try:
        decoded = json.loads(unquote(encoded.strip()))
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    return False, decoded


class CreationActionCommands:
    """Delegate hidden GUI actions to existing deterministic engine primitives."""

    def resolve(self, text: str, locale: str):  # noqa: ANN201 - mirrors CommandRouter.resolve
        stripped = text.strip()
        prefix = next((item for item in self.prefixes if stripped.startswith(item)), "")
        if prefix:
            rest = stripped[len(prefix) :].lstrip()
            token, _separator, args = rest.partition(" ")
            if token.casefold() == _CREATION_ACTION_WORD:
                return _hidden_spec(self.cmd_creation_action), args.strip()
        return super().resolve(text, locale)

    async def cmd_creation_action(self, ctx: CommandCtx) -> str:
        action, separator, payload = ctx.args.strip().partition(" ")
        action = action.casefold()
        if not separator or action not in _ACTIONS:
            return ctx.fail(ctx.i18n.t("commands.error.bad_args"))

        if action == "context":
            parsed_context = _context_payload(payload)
            if parsed_context is None:
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            skip, values = parsed_context
            character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
            if not has_character(character):
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            try:
                pack = load_rulepack(character.system)
                set_character_context(pack, character, values, skip=skip)
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
            except (CharacterContextError, Exception) as exc:
                # CharacterContextError is the expected validation path; the broad
                # boundary also keeps a storage/provider failure from escaping this
                # private service command as an unhandled turn exception.
                del exc
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            return ""

        shadow = copy.copy(ctx)
        # `copy.copy` intentionally shares the events list: if an underlying handler
        # ever emits a semantic event, it remains part of this command reply. Only its
        # human success prose is suppressed.
        shadow.failed = False

        if action == "start":
            parsed = _start_parts(payload)
            if parsed is None:
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            system, profile, name = parsed
            try:
                pack = load_rulepack(system)
                command = own_make_char_word(pack)
            except Exception:
                pack = None
                command = None
            if pack is None or command is None:
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            shadow.command = command
            if profile:
                shadow.args = f"{profile} | {name}" if name else profile
            else:
                shadow.args = name
            rendered = await self.cmd_make_char(shadow, pack)

        elif action == "finalize":
            try:
                request = json.loads(unquote(payload.strip()))
                if not isinstance(request, dict) or set(request) - {"character", "action", "roll", "row_id", "selections"}:
                    raise ValueError("invalid finalization action")
                character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
                if not has_character(character) or request.get("character") != character.name:
                    raise ValueError("stale character")
                pack = load_rulepack(character.system)
                surface = finalization_surface(pack, character, ctx.locale).get("finalization", {})
                selections = None
                if request.get("action") == "roll" and surface.get("can_roll"):
                    shadow.args = "roll"
                elif request.get("action") == "resolve" and surface.get("can_resolve"):
                    status = creation_finalization_status(pack, character)
                    if status is None or request.get("roll") != status.roll or request.get("row_id") != status.row_id:
                        raise ValueError("stale finalization result")
                    selections = request.get("selections")
                    if not isinstance(selections, dict) or not all(
                        isinstance(key, str) and isinstance(value, str) and value.strip()
                        for key, value in selections.items()
                    ):
                        raise ValueError("invalid selections")
                    shadow.args = "resolve"
                else:
                    raise ValueError("unavailable finalization action")
            except Exception:
                return ctx.fail(ctx.i18n.t("commands.finalization.invalid"))
            shadow.command = "finalize"
            rendered = await self.cmd_finalize(shadow, selections=selections)

        elif action == "create":
            shadow.command = "create"
            shadow.args = payload.strip()
            rendered = await self.cmd_create(shadow)

        else:  # advance
            shadow.command = "advance"
            shadow.args = payload.strip()
            rendered = await self.cmd_advance(shadow)

        ctx.failed = shadow.failed
        if shadow.failed:
            return ctx.fail(rendered)
        # Some legacy public handlers return the localized bad-arguments diagnostic
        # without marking their CommandCtx failed. The rich-client lane must not turn
        # that diagnostic into a silent success just because it suppresses prose.
        if rendered == ctx.i18n.t("commands.error.bad_args"):
            return ctx.fail(rendered)
        return ""
