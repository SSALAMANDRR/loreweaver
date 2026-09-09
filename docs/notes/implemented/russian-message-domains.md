Problem: Russian Studio and DH2 labels were mixed with English engine creation messages.
Verdict: Translate Russian engine messages in complete domains, with explicit English fallback elsewhere.
Reason: Pin the initial five domains, preserve key and format-field parity, and add domains incrementally without claiming full translation.
Date: 2026-09-09.
Rule home: [DH2 localization status](../../dh2-port.md#localization-and-distribution), enforced by `tests/i18n/`.
