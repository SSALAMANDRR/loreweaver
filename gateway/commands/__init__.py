"""Platform-independent command router with EN slash and CN SealDice dialects.

A package since 2026-08-19: `router` holds the spec table and dispatch, and each command
domain is its own module composed into `CommandRouter` as a mixin (`checks`, `sheet`,
`rules`, `rooms`, `cast`, `world`, `panels`, `media`, `llm`). Import the public names
from here; monkeypatch a helper where it is DEFINED (e.g. `gateway.commands.llm.flow_for`).
"""

from __future__ import annotations

from gateway.commands.advancement import AdvancementCommands
from gateway.commands.finalization import FinalizationCommands
from gateway.commands.profile_creation import ProfileCreationCommands
from gateway.commands.readiness import ReadinessCommands
from gateway.commands.router import CommandRouter as _BaseCommandRouter
from gateway.commands.types import CommandCtx, CommandReply, CommandSpec


class CommandRouter(
    ReadinessCommands,
    FinalizationCommands,
    AdvancementCommands,
    ProfileCreationCommands,
    _BaseCommandRouter,
):
    """Public router plus generic creation, advancement, finalization and readiness surfaces."""


__all__ = ["CommandCtx", "CommandReply", "CommandRouter", "CommandSpec"]
