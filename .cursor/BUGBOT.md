# Bugbot review guide — Loreweaver

Loreweaver is a self-hosted AI Game Master engine for tabletop RPGs: a
deterministic engine (dice, rules, state) wrapped around model-driven narration.
`AGENTS.md` is the full contributor contract; this file tells a reviewer what to
look for in a diff and what NOT to flag. Cite `file:line` and describe the
concrete failure scenario for every finding. Label each finding as either a
RED LINE (below) or an improvement.

## Red lines — flag any diff that crosses one

1. **Deterministic vs generative.** Dice, success levels, character math, random
   tables, permissions and censorship are real code in `core/`. `core/` makes no
   model calls at all. Flag: a model call added under `core/`; a rule outcome
   (a success level, a stat change, a table result) produced or "interpreted" by
   the model instead of the engine.
2. **Dice-first.** A check rolls real dice, then narrates the outcome per the
   result. Flag: any path that narrates a check result without a dice tool
   having fired, or that lets the model state a roll it did not make
   (`agent/turn_checks` is the runtime guard; do not weaken it).
3. **Information isolation.** Every outbound player-facing surface goes through
   `core.documents.project(doc, viewer)`; NPC and companion actors are built from
   their OWN record and sheet only, never the keeper pool. Flag: a new wire
   path that serializes a `Document` without `project()`; secret lore, NPC
   secrets, keeper-only modvars or unexposed MVU leaves reaching a player-grade
   view; imported world machinery (hooks, `[InitVar]`, EJS) reaching a room by
   any path other than the keeper's `.import … world`. Do NOT propose scoping,
   tiering or staging the main Keeper's module knowledge — the Keeper holding
   the full module is deliberate and permanent (`docs/notes/rejected/keeper-knowledge-scoping.md`).
4. **English-first + i18n.** Identifiers, comments and commits are English.
   Every user-facing string goes through `infra.i18n` with entries in BOTH
   `locales/en/*.json` and `locales/zh/*.json`; client-side through the typed
   `tt()`/`messages` dict in `clients/tui/src/i18n.ts`, both languages. Flag: a
   hardcoded natural-language string on a user-facing path; a locale key added
   to one language only. CJK game DATA (skill names, aliases) is exempt.
5. **One prompt assembler per lane.** The Keeper's context is assembled only in
   `agent/prompt_builder.py` and sent only by `agent/loop.py`. Every other model
   call is a declared lane (NPC/companion/Director/Scribe actors, chronicle
   fold, forge/module-analysis/RAG/persona lanes). Flag: any other module
   building or extending Keeper context; a new `.chat()` call site that is not
   named in `tests/architecture/test_model_call_lanes.py`.

## Budget, locking and lifecycle — the paid-for rules

- One player turn is worst-case **~155 model calls**. A new model-driven path
  must update that number in `AGENTS.md` AND `tests/agent/test_turn_call_budget.py`.
- `hub.turn_lock(session_key)` is taken only at the transport choke points
  (`net/session.py`, `gateway/runner.py`). `run_kp_turn` and the companion
  director deliberately do NOT take it — adding it there self-deadlocks nested
  companion/director sub-turns. Companion sub-turns run no Scribe/Director of
  their own; that guard lives in `gateway/turn.py` (×2) and `gateway/director.py`.
- The Scribe/Director pass runs outside the lock; undo/reset/import/delete/load
  must cancel-and-drain `agent/scribe_coord.py`'s chain before mutating.
- A new `room_state` key, document type or vector lane must declare a
  `RoomStateFacet` in the module that writes it and be listed in
  `net/room_lifecycle.FACET_MODULES`.
- Before lifecycle / locking / provider / replay changes, `docs/defensive-patterns.md`
  applies; check the diff against it.

## Tools, packs and protocol

- A `@tool` that reads secrets must be `keeper_only=True`; skill-unlocked tools
  `gated=True`; bulk prep tools `prep_only=True`; tools whose calls for different
  values of one argument touch different documents `concurrent_by="<arg>"`;
  tools needing a store some rooms lack `needs="<capability>"`. Flag a
  secret-reading tool without `keeper_only`.
- Rule systems are rulepack DATA (`rulepacks/*.yaml`). Flag rule-system-specific
  words or logic added to `core/` or `agent/` (pinned by `tests/architecture/`).
- A protocol bump touches five places at once: `net/session.py`,
  `clients/protocol/src/types.ts`, the npm manifest version + description,
  `clients/protocol/README.md`, `docs/protocol.md`. Flag a partial bump.
- Never re-add chat-platform adapters (Discord/QQ/Telegram/Feishu/OneBot);
  clients build against `docs/protocol.md`.

## Decision records are binding

`docs/notes/rejected/` holds mechanisms the owner rejected; a diff that
reintroduces one (chat adapters, keeper knowledge scoping, chronicle dup
suppression, the sole-active card mechanism, a pack-install shadowed gate) is a
finding regardless of code quality. A non-trivial change should add or update a
note in `docs/notes/` in the same PR — say so if it does not.

## Tests

- The suite is offline and deterministic (`FakeLLM`, `FakeEmbeddings`, seeded
  dice). Flag a test that needs network, keys or wall-clock timing.
- Flag deleted or weakened sentinel tests under `tests/documents/` and
  `tests/architecture/`, and any assertion loosened to make a red test pass.
- Behavioural claims about the live model belong to the nightly real-model eval
  (`scripts/playtest.py`, `scripts/longrun.py`), not to unit tests.

## Do not flag

- File length or function count — there is no line-count rule here.
- Missing backward-compatibility shims: breaking changes are sanctioned
  pre-ecosystem, as long as the commit names what breaks.
- Cosmetic style already covered by `ruff` and `scripts/i18n_lint.py`.
- Comments that state a constraint the code cannot show — those are wanted;
  comments that narrate the change or justify it to the reviewer are not.
