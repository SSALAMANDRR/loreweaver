from types import SimpleNamespace

import pytest

from net.admin import _subscription_login_status


pytestmark = pytest.mark.asyncio


def _services_with_session(session: dict | None = None):
    services = SimpleNamespace()
    if session is not None:
        services._subscription_logins = {"chatgpt": session}
    return services


async def test_subscription_login_status_reports_pending_success_and_failure():
    services = _services_with_session({"done": False, "error": None})
    assert _subscription_login_status(services) == "chatgpt:pending"

    services._subscription_logins["chatgpt"].update(done=True, token_ok=True)
    assert _subscription_login_status(services) == "chatgpt:logged_in"

    services._subscription_logins["chatgpt"].update(
        token_ok=False,
        error="subscription_poll_failed",
    )
    assert (
        _subscription_login_status(services)
        == "chatgpt:error:subscription_poll_failed"
    )


async def test_subscription_login_status_is_empty_without_background_session():
    assert _subscription_login_status(SimpleNamespace()) == ""
