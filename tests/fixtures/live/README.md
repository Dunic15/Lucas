# Live Conversational-Realism Fixtures

This directory contains synthetic, audit-safe meeting transcripts for
`tests/eval_live_realism.py`. Matching turn labels live in
`tests/fixtures/expected/live/<scenario>.json`.

Run the offline eval from the repository root:

```sh
.venv/bin/python tests/eval_live_realism.py
```

The driver pins `BRAIN_PROVIDER=stub`, `BRAIN_PROVIDER_POST=stub`, and
`EMBEDDINGS_PROVIDER=hash` before importing backend modules. No keys, network,
real people, customers, or customer transcripts are used.

## Transcript format

Each non-empty line is one final human utterance:

```text
Synthetic Speaker: Utterance text.
```

The expected JSON has a synthetic roster, coverage tags, and one label object
per transcript turn in the same order. Every turn annotates all six dimensions:

- `floor_holding`: `expected` is `hold` or `yield`; `deference` is `short`,
  `medium`, `base`, or `long`. Optional `active_partial: true` simulates another
  live ASR partial arriving inside the deference window.
- `address_detection`: `expected` is `speak` or `silent`. `model_result` is fed
  unchanged to the real `passes_confidence` seam. The decision twin applies the
  real wake and other-addressee gates before deciding whether speech is allowed.
- `multiparty_deference`: expected target is `laura`, a full synthetic roster
  name, `room` for a room-open question, or `none` for no conversational target.
- `control_intent`: `none`, `stop`, `leave`, `invite`, or `closing`.
- `emotional_appropriateness`: the expected `classify` label plus the exact
  `talk_mood` and `ditto_emo` renderer mappings.
- `intervention_timing`: `nudge` only on the first genuine closing intervention;
  otherwise `silent`.

A turn may contain an `xfail` object mapping one dimension to a one-line runtime
bug reason. Only that observation is excluded from the score; the other five
dimensions on the same turn remain live regression checks. The standalone
scorecard prints every xfail and includes it in the machine JSON blob.

Precision and recall are computed per dimension. The composite gives address
precision the largest weight and multiparty target precision the next largest;
both use F0.5, so false speech and wrong-target predictions cost more than misses.
