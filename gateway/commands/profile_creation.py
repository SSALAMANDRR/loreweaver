"""Generic command wiring for pack-declared character-creation profiles.

The base command layer already knows how to run a pack's ordinary ``make_char``
entry point. This mixin adds one generic branch for packs that declare
``creation_constraints.profiles``: the player selects a profile and the same
make-char command creates the sheet through ``core.creation_profiles``.

No profile vocabulary lives here. A pack may use the primitive for home worlds,
ancestries, careers, templates, or any other mutually selected creation option.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.advancement_purchase import initialize_advancement_budget
from core.character_manager import CharacterNameTakenError
from core.character_rules import render_validation_notice, validate_sheet
from core.creation_profiles import (
    CreationProfileError,
    generate_profiled_character,
    resolve_creation_profile,
)
from core.rulepacks import RulePack
from gateway.commands.types import CommandCtx


def _profile_map(pack: RulePack) -> Mapping[str, object]:
    raw = (pack.creation_constraints or {}).get("profiles") or {}
    return raw if isinstance(raw, Mapping) else {}


def _parse_profile_make_char_args(pack: RulePack, args: str, default_name: str) -> tuple[str, str]:
    """Parse ``<profile> [| <character name>]`` for a profiled make-char pack.

    Multi-word profile aliases use the explicit ``|`` separator. Canonical
    single-token ids may omit it and put the character name after the id. A
    bare profile alias creates the locale-default character name.
    """

    raw = args.strip()
    if not raw:
        raise CreationProfileError("a creation profile is required")

    profile_text, separator, name_text = raw.partition("|")
    if separator:
        resolved = resolve_creation_profile(pack, profile_text.strip())
        if resolved is None:
            raise CreationProfileError(f"unknown creation profile: {profile_text.strip()}")
        return resolved[0], name_text.strip() or default_name

    resolved = resolve_creation_profile(pack, raw)
    if resolved is not None:
        return resolved[0], default_name

    first, space, rest = raw.partition(" ")
    resolved = resolve_creation_profile(pack, first)
    if resolved is not None:
        return resolved[0], rest.strip() or default_name

    raise CreationProfileError(f"unknown creation profile: {raw}")


class ProfileCreationCommands:
    """Outer command mixin that augments the existing generic make-char flow."""

    async def cmd_make_char(self, ctx: CommandCtx, pack: RulePack | None = None) -> str:
        if pack is None:
            pack = await ctx.services.room_rulepack(ctx.raw_ctx)

        if not _profile_map(pack):
            return await super().cmd_make_char(ctx, pack)

        default_name = ctx.i18n.t("commands.character.default_name")
        budget = None
        try:
            profile_id, name = _parse_profile_make_char_args(pack, ctx.args, default_name)
            generated = generate_profiled_character(
                pack,
                profile_id,
                name,
                roller=ctx.services.dice,
            )
            budget = initialize_advancement_budget(pack, generated.character)
        except CreationProfileError:
            return ctx.i18n.t("commands.error.bad_args")

        character, violations = validate_sheet(
            generated.character,
            pack.system,
            initialize_vitals=True,
            creation_method="rolled",
        )
        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except CharacterNameTakenError:
            return ctx.fail(
                ctx.i18n.t("commands.character.name_taken", name=character.name, command=ctx.command)
            )

        lines = [ctx.i18n.t("commands.character.created", name=character.name, system=character.system)]
        if budget is not None:
            lines.append(
                ctx.i18n.t(
                    "commands.advancement.creation_hint",
                    available=budget.available_xp,
                )
            )
        notice = render_validation_notice(ctx.i18n, violations)
        if notice:
            lines.append(notice)
        return "\n".join(lines)
