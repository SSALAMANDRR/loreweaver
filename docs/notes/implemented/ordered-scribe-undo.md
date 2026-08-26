# Implemented: ordered Scribe chain, lifecycle cancel, snapshot refresh

- **Problem:** the post-turn Scribe was a bare `asyncio.create_task` per player
  turn. Same-room passes could finish out of LLM order; `.undo` / `.reset` /
  import / delete / `.save load` could restore or wipe a room a still-running
  pass then wrote back; and `run_kp_turn` photographed the turn boundary
  *before* the pass, so tracker / habit / chronicle / whisper / Director writes
  sat in no correct rewind snapshot. `except Exception` around the pass looked
  like it would treat a lifecycle cancel as a logged bookkeeping failure.
- **Decision:** one in-process coordinator (`agent/scribe_coord.py`) owns a
  per-room chain. Two operations, two homes — not a copy in every command:
  `gateway.turn` *schedules* after the reply has already streamed;
  `agent.undo.restore` and `net.room_backup` reset/import/delete
  *cancel-and-drain* before they mutate. A completed pass refreshes the
  snapshot named by the *current* chronicle counter, and only while that
  counter still names the turn the pass was scheduled on. Room delete and
  `.reset all` drop the slot.
- **Cut in review (2026-08-24): nothing waits on the chain.** The first draft
  also had the next external KP turn await the previous turn's pass before
  assembling its prompt, so turn-N whispers were guaranteed to reach turn N+1.
  That guarantee is not free: it hauls the previous turn's Scribe call, its
  Director call and its image into the next turn's lock, so the latency
  fire-and-forget hid comes back one turn later — against the 2026-08-21
  latency ruling. Whispers reach the next prompt when the pass has landed, and
  the turn after that otherwise. Ordering the chain is the part that was
  actually broken; waiting on it was a second, costlier feature riding along.
- **Reason (why the cancel entries sit where they do):** cancel-and-drain
  cannot live at the transport choke point — that path must not learn command
  names (the turn pipeline no longer does), so each destructive operation
  quiesces at its own mutation entry. Companion sub-turns re-enter the turn
  flow inside the same player turn and they advance the counter, which is why
  a refresh must not write `result.turn`. The coordinator stays in `agent/` so
  the undo restore does not grow a gateway/net reverse import.
- **Amendment (2026-08-26): a turn that committed nothing gets no pass.** The
  scheduling guard was a bare `if result is not None`, but `run_kp_turn` also
  RETURNS on a provider error — a diagnosis carrying `turn == 0`, with no
  history persisted and the chronicle counter unmoved. The pass ran for that
  dead turn too, and `refresh_latest_snapshot` re-photographed the UNMOVED
  counter's boundary, welding the failed attempt's `turn_start` hook writes,
  the tool calls that landed before the provider died, and the pass's own
  whisper into the previous turn's snapshot — which `.undo` could then no
  longer remove. `run_scribe_pass` now returns early on `result.turn <= 0`,
  beside the companion gate rather than at the two call sites, so the hub path
  and the inline CLI path (`gateway.runner`) cannot drift. Same line the
  auto-chronicle lane already drew.
- **`CancelledError` note:** it derives from `BaseException`, so the
  `except Exception` guard around the pass never caught it in the first place.
  The property the note claims is real; the `except asyncio.CancelledError:
  raise` blocks that were meant to enforce it were unreachable and are gone.
- **Rule home:** `agent/scribe_coord.py` module docstring; AGENTS.md per-turn
  budget paragraph (the lane outside the lock); `docs/defensive-patterns.md`
  entry 7.
- **Date:** 2026-08-22 (review trim 2026-08-24).
