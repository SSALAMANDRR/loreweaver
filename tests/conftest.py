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

import importlib.util
import inspect
import os
import shutil

import pytest

os.environ.setdefault("TRPG_SCRIBE__ENABLED", "0")
os.environ.setdefault("TRPG_CHRONICLE__ENABLED", "0")

_POSIX_SEMANTIC_TESTS = {
    "tests/agent/test_dispatch_guards.py::test_tool_trace_records_every_dispatched_call_when_an_operator_asks",
    "tests/agent/test_dispatch_guards.py::test_tool_trace_file_is_private_from_its_very_first_byte",
    "tests/infra/test_media_store.py::test_commit_fsyncs_blob_and_directory_before_publishing_metadata",
}

_ANTHROPIC_TESTS = {
    "test_build_llm_selects_anthropic",
    "test_anthropic_base_url_drops_openai_style_v1_suffix",
}

_GEMINI_TESTS = {
    "test_build_llm_selects_gemini",
    "test_gemini_streaming_keeps_the_usage_it_was_given",
    "test_to_gemini_tools_maps_function_declaration_with_clean_schema",
}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def pytest_collection_modifyitems(config, items):  # noqa: ANN001, ANN201
    """Keep the suite honest about platform semantics and optional provider extras.

    Windows is a supported runtime, not a pretend POSIX host. Exact chmod(0600)
    and directory-fsync assertions therefore stay POSIX-only; Windows runtime
    behavior is covered by Windows-specific storage tests. Optional provider SDK
    tests run when their advertised extras are installed and skip otherwise.
    Bash-installer execution tests similarly skip only when no bash is present;
    static PowerShell/cross-installer checks in the same module still run.
    """
    del config
    bash_available = shutil.which("bash") is not None
    anthropic_available = _module_available("anthropic")
    gemini_available = _module_available("google.genai")

    for item in items:
        normalized = item.nodeid.replace("\\", "/")
        base_nodeid = normalized.split("[", 1)[0]

        if os.name != "posix" and base_nodeid in _POSIX_SEMANTIC_TESTS:
            item.add_marker(pytest.mark.skip(reason="asserts POSIX chmod/directory-fsync semantics"))
            continue

        if item.path.name == "test_providers.py":
            if item.name in _ANTHROPIC_TESTS and not anthropic_available:
                item.add_marker(pytest.mark.skip(reason="optional anthropic SDK is not installed"))
                continue
            if item.name in _GEMINI_TESTS and not gemini_available:
                item.add_marker(pytest.mark.skip(reason="optional google-genai SDK is not installed"))
                continue

        if item.path.name == "test_client_installers.py" and not bash_available:
            try:
                source = inspect.getsource(item.obj)
            except (OSError, TypeError):
                source = ""
            if "_run_installer(" in source or "[\"bash\"" in source or "['bash'" in source:
                item.add_marker(pytest.mark.skip(reason="bash installer execution requires bash"))


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
