"""Suite-wide defaults.

The post-turn Scribe (`agent.scribe`) is ON in production but OFF for the test
suite: its fire-and-forget extra LLM call would make every FakeLLM call-count
assertion nondeterministic (and racy — the task lands whenever the loop yields).
`tests/agent/test_scribe.py` opts back in explicitly on its own services.

The chronicle fold (`agent.chronicle`, M18) follows the same posture: ON in
production, OFF here, because its fold-generation LLM call fires from inside
`run_kp_turn` whenever the room's usage meter crosses the trigger — fatal to
unrelated call-count assertions the moment a test seeds chronicle records.
`tests/agent/test_chronicle.py` / `tests/gateway/test_chronicle_commands.py`
opt back in explicitly on their own services.
"""

import os

import pytest

os.environ.setdefault("TRPG_SCRIBE__ENABLED", "0")
os.environ.setdefault("TRPG_CHRONICLE__ENABLED", "0")


@pytest.fixture(autouse=True)
async def _fresh_scribe_chain():
    """Give every test its own Scribe coordinator state.

    `agent.scribe_coord.scribe_runtime` is a PROCESS-level singleton, and each room
    slot holds an `asyncio.Lock`. pytest-asyncio gives each test its own event loop,
    and a lock created on a dead loop poisons every later test in the same process
    that schedules a pass for that room — which is any test that drives
    `gateway.turn` or `net.room_backup`, not only the coordinator's own file. So the
    reset lives here, suite-wide, rather than beside the tests that first needed it.
    """
    from agent.scribe_coord import scribe_runtime

    await scribe_runtime.reset_for_tests()
    yield
    await scribe_runtime.reset_for_tests()
