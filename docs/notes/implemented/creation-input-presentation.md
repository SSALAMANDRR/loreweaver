# Creation text-input presentation

Problem: Free creation choices exposed a name and an unexplained text box; specialized
options exposed only a generic specialization label.
Decision: Rulepacks author localized input labels, placeholders and descriptions in the
creation presentation sidecar. The engine projects optional input metadata for free
choice groups and specialized options through protocol 2.5. Studio renders it with
localized generic fallbacks and accessible descriptions, without system-specific logic.
Mechanics, input encoding, specialization normalization and ranks remain unchanged.
Validation: presentation loader/resolver, state projection and Studio interaction tests;
existing creation mechanics tests and the cross-repo roundtrip gate.
Date: 2026-09-21.
Rule home: core/creation_presentation.py, core/creation_surface.py, clients/protocol/src/creation.ts.
