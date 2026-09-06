"""Generic readiness state for characters created through managed creation flows.

Legacy sheets remain playable: readiness enforcement activates only when a sheet
actually carries staged-creation state. Once a managed flow exists, the sheet is
not ready for character-driven play until the flow and any pack-declared
mandatory finalization are complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.creation_finalization import creation_finalization_status, load_creation_finalization_spec
from core.creation_flow import creation_flow_status, load_creation_flow_spec


@dataclass(frozen=True)
class CharacterReadiness:
    ready: bool
    managed: bool
    phase: str
    blocked_reference: str = ""


def character_readiness(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> CharacterReadiness:
    """Return whether a character may enter normal character-driven play.

    A pack gaining creation-flow support must not invalidate existing campaign
    sheets, so a sheet without persisted flow state is treated as a legacy ready
    character. A sheet that does carry flow state is managed by the new lifecycle
    and must finish every declared stage and finalization.
    """

    if load_creation_flow_spec(pack, data_root=data_root) is None:
        return CharacterReadiness(ready=True, managed=False, phase="ready")

    flow = creation_flow_status(pack, character, data_root=data_root)
    if flow is None:
        return CharacterReadiness(ready=True, managed=False, phase="ready")
    if not flow.complete:
        return CharacterReadiness(ready=False, managed=True, phase="creation")

    if load_creation_finalization_spec(pack, data_root=data_root) is None:
        return CharacterReadiness(ready=True, managed=True, phase="ready")

    finalization = creation_finalization_status(pack, character, data_root=data_root)
    if finalization is None:
        return CharacterReadiness(ready=False, managed=True, phase="finalization")
    if not finalization.complete:
        if finalization.blocked_reference:
            return CharacterReadiness(
                ready=False,
                managed=True,
                phase="blocked",
                blocked_reference=finalization.blocked_reference,
            )
        return CharacterReadiness(ready=False, managed=True, phase="finalization")

    return CharacterReadiness(ready=True, managed=True, phase="ready")
