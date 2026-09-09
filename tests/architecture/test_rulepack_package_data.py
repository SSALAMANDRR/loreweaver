"""Installed rulepacks must retain all data consumed by the generic loaders."""

import tomllib
from pathlib import Path


def test_package_data_includes_every_builtin_rulepack_and_sidecar():
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["rulepacks"]
    pack_root = root / "rulepacks"
    included = {path for pattern in patterns for path in pack_root.glob(pattern)}
    required = set(pack_root.rglob("*.yaml"))
    assert required
    assert not required - included, sorted(str(path.relative_to(root)) for path in required - included)
