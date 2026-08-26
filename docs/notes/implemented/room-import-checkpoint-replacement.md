# Implemented: room import is a full checkpoint replacement

- **Problem:** `import_room` upserted the snapshot's KV / documents / `room_state`
  / media and only deleted legacy aliases of incoming vector ids. Live rows the
  snapshot did not name survived a `.save load`. History was already
  delete-then-append, but `_capture_room_state` / `_rollback_room_state` omitted
  the tree, so a later-leg failure restored documents/state/vectors/media/keys
  and left chat history on the imported snapshot.
- **Verdict:** a load replaces every exportable storage that carries campaign
  content. Success leaves the target room identical to the snapshot on
  documents, `room_state`, history, vectors and media. Failure rolls every
  mutated storage — including history — back to the pre-import capture. Foreign
  `bound_room.*` protection, media staged/created compensation, attempt-every-leg
  rollback, and the undo-ring clear/restore stay as they were.
- **Reason:** a named save is a whole-room checkpoint. Merge semantics make
  "the room after load" a mixture of two lives; a failed load that keeps the
  new history is the same tear in the other direction.
- **Two things a load does NOT replace (review amendment, 2026-08-24):**
  - Bearer keys AND `bound_room.*` bindings stay restore-only. Both are wiring,
    not campaign content: a key says who may open the door, a binding says which
    platform conversation the door leads to. A load rewrites the story people
    walk into, never the way they get in, so a key minted or a table bound after
    the save was taken survives it. (The foreign-binding conflict still fails
    closed — that is a different question: a snapshot row naming an identity now
    bound elsewhere.)
  - A missing or corrupt EXTRA media blob is logged and skipped rather than
    fatal. Extras are staged only so a failed leg can put them back, and the
    load was going to drop that blob anyway — one bad file must not lock a room
    out of every snapshot it has. `delete_room_data` keeps the hard failure:
    there the blob is one the operation promises to be able to hand back.
- **Amendment (2026-08-26): the extras leave the index before the snapshot's
  media are written.** Staging only moved the extras' BLOBS aside; their
  `media_index` rows survived until after the snapshot's media had been
  committed, so they kept counting against the room quota during that write. A
  room that cleared its media and refilled the quota with new files could
  therefore no longer load its own older save — the snapshot's hashes were no
  longer indexed to short-circuit the duplicate check, and the extras the load
  was about to delete pushed it over `media_quota_exceeded`. The drop now runs
  first, which keeps it inside the same compensation rather than outside it:
  `_restore_staged_media` already returns both halves, bytes and index rows, on
  a later leg's failure. `delete_room_data` is untouched.
- **Rule home:** `net/room_backup.py` (`import_room`, `_replace_room_*`,
  `_rollback_room_state`); AGENTS.md lifecycle paragraph (`room_backup` owns
  order and atomicity).
- **Date:** 2026-08-22.
