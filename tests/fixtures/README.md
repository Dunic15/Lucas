# Laura Onboarding Eval Fixtures

This fixture set supports the lightweight synthetic eval suite in
`tests/eval_laura_onboarding.py`. The eval runs Laura's real MeetingState and
post-meeting artifact path in offline stub mode, then checks whether synthetic
customer onboarding meetings produce the expected missing process steps,
readiness score, action items, risks, and proceed/no-proceed decision.

Run from the repo root:

```sh
.venv/bin/python tests/eval_laura_onboarding.py
```

The synthetic transcript fixtures live in `tests/fixtures/meetings/`:

- `dpa_missing.txt`
- `security_approval_missing.txt`
- `implementation_owner_missing.txt`
- `go_live_date_missing.txt`
- `all_complete.txt`
- `ambiguous_owner_not_assigned.txt`
- `dpa_legal_handle_not_confirmed.txt`
- `security_approval_completed.txt`

Expected outputs live in `tests/fixtures/expected/` with matching JSON file
names. Each expected file defines `expected_missing_steps`, an
`expected_readiness_score` range, `expected_actions`, `expected_risks`, and
`should_proceed`.

Use synthetic data only. Do not add real customer transcripts, names, secrets,
or PII to these fixtures.
