Problem: Studio ended staged creation before the engine's mandatory finalization was resolved.
Verdict: Protocol 2.4 exposes connection-local readiness and finalization, with server-authorized roll/resolve actions.
Reason: Clients display pending choices and preserved blocked results without reproducing rulepack mechanics; legacy sheets stay playable.
Date: 2026-09-09.
Rule home: [wire contract](../../protocol.md#character-creation-and-finalization-v24), implemented by `core/finalization_surface.py` and the existing command handlers.
