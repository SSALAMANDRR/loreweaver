"""Production entrypoints must not turn the live dice stream into a test fixture."""

from __future__ import annotations

import ast
from pathlib import Path


def test_app_entrypoint_has_no_literal_fixed_dice_seed() -> None:
    """Tests may seed rolls explicitly; live CLI/serve startup may not.

    A literal ``seed_dice(N)`` in ``app.py`` resets every process restart to the
    same sequence, so character generation and every later automatic roll repeat
    across restarts. A future opt-in/configurable seed may pass a variable instead.
    """

    tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    fixed_calls: list[int | float | str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "seed_dice" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant):
            fixed_calls.append(argument.value)

    assert fixed_calls == []
