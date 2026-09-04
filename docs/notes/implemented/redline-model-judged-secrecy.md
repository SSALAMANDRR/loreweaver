# Implemented: the red-line secrecy gate is model-judged, not string-matched

- **Problem:** the nightly gate's leak scoring was two string channels — verbatim
  snippets pulled from EVERY keeper-pool leaf, and word-boundary concept sentinels —
  and it was wrong in both directions, each direction observed live. False
  positives: run 32928889621 flagged the Keeper for narrating PUBLIC tavern
  dressing ("A water-stained harbor map hangs by the hearth.") because the snippet
  extractor treated all pool text as secret; run 32688930136 flagged "the deep ones
  going soft" describing rake marks in wet sand; runs 32330359502/31925662707
  flagged chronicle records that faithfully summarized the village pact AFTER the
  players earned it from Martha under interrogation the module itself marks "to be
  earned". False negatives: the fixture's central secret ("the lighthouse keeper is
  the murderer; something wears his face") contains none of the five sentinel
  words, so narrating it outright was invisible. Red nights clustered on sessions
  where play went WELL — the gate punished reaching the earned reveal.
- **Decision (owner, 2026-08-26): leak-or-not is the judge model's call, whole.**
  Whether a sentence conveys unearned secret knowledge is a semantic question about
  the material and the play history; string rules answer neither, and per-module
  sentinel word lists have no generality. `judge_secrecy` (scripts/playtest.py,
  shared by both harness lanes) rules on EVERY player-facing text — each KP reply
  and each chronicle player projection — given: the module's secret material
  (`extract_secret_material`: ONLY the pool fields `_build_knowledge_pools`
  designates as secrets — scene `keeper_notes`, NPC `secret`, `truths`, `threats`,
  `timeline`; never scene text, NPC descriptions, or `clues`), the recent
  transcript (so an earned or already-known reveal is not a leak), and
  `--secret-concepts` demoted to optional attention hints. Verdicts are strict
  JSON with mandatory evidence: a leak must QUOTE the leaking sentence, a pass
  must name what earns it — the same contract as the dice judge and the Scribe's
  evidence gate.
- **Fail-closed, and red nights stay explainable:** an unreachable or unusable
  judge counts as a leak (the gate must not go soft when the judge is down), and
  the new `judge_failures` counter alongside `judged_texts` in the summary says
  whether a red night means "the Keeper leaked" or "the judge was down". Every
  verdict lands in the JSONL (`SECRECY_VERDICT` / `CHRONICLE_SECRECY_VERDICT`);
  confirmed LEAK records now carry the whole reply (the old 200-char stubs made
  every triage start from re-running the night). A run whose secret material
  extracts to empty counts as an error — a gate that judged nothing must not
  report green.
- **Cost:** one small judge call per non-empty player-facing text — ≈24 replies +
  ≈23 chronicle records (playtest) + ≈30 replies (longrun) per night, a ~20-30%
  token add on the run; thresholds, zero tolerance, and the dice channels are
  untouched.
- **Risk stance:** this trades documented false positives for possible judge
  false-greens. Accepted: the judge reads the secret material itself (not word
  proxies), every verdict is auditable with its quoted evidence, and the README
  already scopes the nightly's claim to "per model, per run, not a standing
  guarantee". Keeper narration that names an unidentified secret entity (the
  "The thrall has gone silent" turn, run 32688930136) is now judged on the
  merits — if the judge confirms it, that is a behavioral finding, not eval noise.
- **Recalibration after the judge's first live run (owner, same day; run
  32953952032):** the initial "anything from the material, unless earned"
  framing policed PACING, not secrecy — 12 of 14 verdicts flagged legitimate
  keeping: the Keeper staging the module's own Night-1 light-shift event, and
  "the map hangs crooked" signposting that a clue EXISTS. Two changes, both
  owner-approved: `timeline` left the secret material (it is the Keeper's event
  schedule — WHEN events fire is the Keeper's call), and the criteria now draw
  the line at CONTENT-level disclosure (hidden identities, mechanisms,
  solutions, what an unfound clue says) with the exempt craft named — staging /
  foreshadowing events, atmosphere, existence-only signposting. The same run's
  two REAL leaks (an NPC naming the secret species unprompted; narration naming
  an unfound clue's content) stay leaks under this line — the gate catching
  them is the gate working. Hardening in the same batch: the judge prompt is
  built by concatenation, never str.format — the material/transcript/reply are
  untrusted brace-laden content ({1D6} dice notation, JSON), and a template
  pass is one refactor from a crash a whole night would wear as "judge down" —
  and prompt construction moved inside the fail-closed boundary (an escape
  there was a TURN_ERROR in one lane and an unhandled crash in the longrun
  lane, not a verdict).
- **Second recalibration (owner, 2026-09-04) after seven consecutive red
  nightlies (2026-08-27..09-03; 43 confirmed verdicts, zero judge failures):**
  23 verdicts quoted the lighthouse light turning green and 9 the tide table
  behind the map -- both PUBLIC scene lines of the fixture that the module
  ANALYZER (a model) had filed under Martha's `secret` and the scene's
  `keeper_notes`, from where the judge dutifully treated them as hidden truths;
  11 quoted truth-tier content (the pact at turn 2, "Elias drowned", "the light
  calls") and are real Keeper leaks, now tracked in
  `pending/keeper-volunteers-hidden-truths.md` (plus the Scribe's anticipatory
  records in `pending/scribe-anticipatory-records.md`). Lesson: what the judge
  is told is secret decides what it flags; exemption lists cannot persuade it
  past its material. Verdicts: (1) the material is the truth tier only --
  `truths` + `threats`; `keeper_notes` and NPC `secret` are gone, accepting that
  a truth written only in an NPC secret may slip through, because this eval
  prefers a false green to a false red; (2) the judge returns a CATEGORY --
  `truth` / `pacing` / `none` -- and only `truth` is gated; `pacing` (a clue,
  an NPC or an event ahead of its cue) is counted and reported, never a red
  line, so an absolutist judge can inflate a metric but not fail the night;
  (3) the judge runs as the Keeper's equal -- same client, model,
  `reasoning_effort`, and the Keeper's configured temperature instead of a
  pinned 0; (4) the material is logged whole per session (`secrecy_material`),
  because this week's root cause was invisible without it; (5) the nightly
  moved from 03:17 UTC -- inside DeepSeek's weekday peak window -- to 18:17 UTC
  (02:17 Asia/Shanghai, off-peak, half price). Expected steady state: red on
  the nights the Keeper actually volunteers a truth, green otherwise, until
  the Keeper-side pending note lands.
