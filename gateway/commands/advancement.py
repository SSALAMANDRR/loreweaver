"""Generic player command for pack-declared advancement budgets and purchases."""

from __future__ import annotations

from core.advancement_purchase import AdvancementPurchaseError, load_advancement_purchase_spec
from core.advancement_surface import (
    available_advancement_surface,
    initialized_advancement_budget,
    safe_purchase_advancement,
)
from core.character_manager import has_character
from core.rulepacks import load_rulepack
from gateway.commands.types import CommandCtx, CommandSpec


def _target_label(pack, target: str, locale: str) -> str:
    family, separator, specialization = str(target).partition("::")
    if separator:
        return f"{pack.display_name(family, locale)} ({specialization})"
    return pack.display_name(target, locale)


def _is_talent(category: str) -> bool:
    return str(category).strip().casefold() == "talent"


class AdvancementCommands:
    """Add one generic `.advance`/`.xp` surface to the public router."""

    def _static_specs(self) -> list[CommandSpec]:
        specs = super()._static_specs()
        specs.append(
            CommandSpec(
                "advance",
                self.cmd_advance,
                ["advance", "xp"],
                ["advance", "xp"],
                {"name": "advance"},
                "commands.help.advance",
                private_reply=True,
            )
        )
        return specs

    async def cmd_advance(self, ctx: CommandCtx) -> str:
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.advancement.no_character"))

        try:
            pack = load_rulepack(character.system)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.advancement.unsupported"))

        if load_advancement_purchase_spec(pack) is None:
            return ctx.fail(ctx.i18n.t("commands.advancement.unsupported"))

        budget = initialized_advancement_budget(pack, character)
        if budget is None:
            return ctx.fail(ctx.i18n.t("commands.advancement.uninitialized"))

        raw = ctx.args.strip()
        if not raw:
            surface = available_advancement_surface(pack, character)
            if surface is None:
                return ctx.fail(ctx.i18n.t("commands.advancement.uninitialized"))
            lines = [
                ctx.i18n.t(
                    "commands.advancement.header",
                    available=surface.budget.available_xp,
                    spent=surface.budget.spent_xp,
                    starting=surface.budget.starting_xp,
                )
            ]
            if not surface.purchases:
                lines.append(ctx.i18n.t("commands.advancement.none"))
                return "\n".join(lines)

            for quote in surface.purchases:
                label = _target_label(pack, quote.target, ctx.locale)
                suffix = (
                    ctx.i18n.t("commands.advancement.unaffordable")
                    if quote.cost > surface.budget.available_xp
                    else ""
                )
                if _is_talent(quote.category):
                    lines.append(
                        ctx.i18n.t(
                            "commands.advancement.talent_item",
                            category=quote.category,
                            target=label,
                            stage=quote.stage,
                            cost=quote.cost,
                            suffix=suffix,
                        )
                    )
                else:
                    lines.append(
                        ctx.i18n.t(
                            "commands.advancement.item",
                            category=quote.category,
                            target=label,
                            stage=quote.stage,
                            current=quote.current_value,
                            next=quote.next_value,
                            cost=quote.cost,
                            suffix=suffix,
                        )
                    )
            return "\n".join(lines)

        category, separator, target = raw.partition(" ")
        if not separator or not target.strip():
            return ctx.fail(ctx.i18n.t("commands.advancement.usage"))

        try:
            result = safe_purchase_advancement(pack, character, category, target.strip())
        except AdvancementPurchaseError:
            return ctx.fail(ctx.i18n.t("commands.advancement.invalid"))

        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.advancement.save_failed"))

        label = _target_label(pack, result.quote.target, ctx.locale)
        if _is_talent(result.quote.category):
            return ctx.i18n.t(
                "commands.advancement.talent_purchased",
                target=label,
                stage=result.quote.stage,
                cost=result.quote.cost,
                remaining=result.remaining_xp,
            )
        return ctx.i18n.t(
            "commands.advancement.purchased",
            target=label,
            stage=result.quote.stage,
            cost=result.quote.cost,
            remaining=result.remaining_xp,
            current=result.quote.current_value,
            next=result.quote.next_value,
        )
