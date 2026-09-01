"""Speaker tag on the Keeper's player line (issue #29).

The gateway prefixes the same display name the room echo uses, so the model and
the persisted history can tell two humans apart. Retrieval (worldbook keys,
chronicle recall) and ``turn_start`` hooks must NOT see that tag: a PC name or
platform nickname that collides with a lore key would otherwise inject every
turn. ``player_line_body`` is the inverse.
"""

from __future__ import annotations

import re

from infra.i18n import I18n

# Inverse of `prompt.speaker_line` (`[{name}]\n{text}`). Names are `_display_name`
# values (no `]` / newline).
_SPEAKER_LINE_PREFIX = re.compile(r"^\[[^\n\]]+\]\n")


def attributed_player_line(i18n: I18n, name: str, text: str) -> str:
    """The player line the Keeper persists and replays — speaker tagged, body intact.

    The room echo carries ``name`` on ``player_action`` separately, so this wrap is
    for the model only. Empty ``name`` leaves ``text`` unchanged (direct ``run_kp_turn``
    callers and tests stay verbatim).
    """
    speaker = name.strip()
    if not speaker:
        return text
    return i18n.t("prompt.speaker_line", name=speaker, text=text)


def player_line_body(text: str) -> str:
    """The player-authored body of a Keeper user line.

    Inverse of ``attributed_player_line``. A line that was never tagged is
    returned unchanged.
    """
    match = _SPEAKER_LINE_PREFIX.match(text)
    return text[match.end() :] if match else text
