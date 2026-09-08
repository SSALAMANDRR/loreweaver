"""Connection-local locale switching shared by every Loreweaver carrier.

The transport-neutral :class:`net.session.SessionCore` deliberately owns the game/session
protocol. This thin subclass adds one presentation concern: a connected rich client may change
its locale without reconnecting or mutating room state. The locale lives on the member, so two
people at the same table may receive independently localized state snapshots.
"""

from __future__ import annotations

import re
from typing import Any

from gateway.hub import Event
from gateway.turn import state_for_ctx
from infra.i18n import get_i18n
from net.session import SessionCore, error_frame, parse_frame

_LOCALE_RE = re.compile(r"^[a-z]{2,8}$")


def normalize_client_locale(value: str) -> str:
    """Collapse common language tags (``ru-RU``, ``zh_CN``) to catalog ids."""
    return value.strip().lower().replace("_", "-").split("-", 1)[0]


class LocalizedSessionCore(SessionCore):
    """SessionCore plus the additive ``locale`` client frame.

    ``{"type": "locale", "locale": "ru"}`` changes only the issuing connection's
    presentation locale and immediately returns a freshly localized state frame. It never writes
    room/game state and never broadcasts another member's language choice to the table.

    A locale does not have to own a global ``locales/<id>`` catalog: rulepacks may provide
    presentation text for languages the core has not translated yet. Generic strings then use
    the normal i18n English fallback while rulepack-authored labels can already use that locale.
    """

    async def _on_frame(self, member: Any, raw: Any) -> None:
        frame = parse_frame(raw)
        if frame is None or frame.get("type") != "locale":
            await super()._on_frame(member, raw)
            return

        i18n = get_i18n(member.locale)
        if not self._refresh_member_authorization(member):
            await member.send_frame(error_frame("forbidden", i18n))
            return

        raw_locale = frame.get("locale")
        if not isinstance(raw_locale, str):
            await member.send_frame(error_frame("bad_frame", i18n))
            return
        locale = normalize_client_locale(raw_locale)
        if not _LOCALE_RE.fullmatch(locale):
            await member.send_frame(error_frame("bad_frame", i18n))
            return

        member.locale = locale
        snapshot = await state_for_ctx(self.hub, self.services, self._ctx_for(member))
        await member.deliver(Event.state(snapshot))
