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
