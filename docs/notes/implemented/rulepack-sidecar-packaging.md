Problem: The wheel's explicit rulepack data patterns omitted nested creation and advancement sidecars.
Verdict: Package every built-in YAML sidecar, including nested creation fragments.
Reason: Installed DH2 must retain the same rule data as the source checkout; no system-specific packaging list is needed.
Date: 2026-09-09.
Rule home: [package-data](../../../pyproject.toml), guarded by `tests/architecture/test_rulepack_package_data.py`.
