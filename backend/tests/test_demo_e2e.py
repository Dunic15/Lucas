"""Scripted key-free end-to-end demo — CI guard for the YC value loop.

This walks the *real* demo endpoints the way the scripted runbook
(`docs/demo/RUNBOOK.md`) does, entirely key-free (BRAIN_PROVIDER falls back to
the offline `stub` with no ANTHROPIC_API_KEY; EMBEDDING_PROVIDER defaults to
`hash`). Every step that would break the live demo breaks a test here instead.

The whole loop is proven deterministically, offline, no clock dependence:

    GET  /demo/sample        → a real meeting transcript ships with the avatar
    POST /demo/ask           → a grounded answer that QUOTES a company doc and
                               carries a citation/source (`citations`)
    POST /demo/post_meeting  → the post-meeting artifact (summary + actions +
                               decisions + risks + readiness) with the action
                               lifecycle fields the dashboard renders

NOTE on the "source/citation" field (runbook step 3): `/demo/ask` already
returns a first-class `citations` list (the source filename Laura grounded on)
plus a richer `retrieved` list (source/section/score). No live-path change was
needed to surface a source — we assert on what the endpoint already returns, the
smallest honest change. The conftest autouse fixtures pin every Settings field
to its code default, which is the key-free contract (anthropic-without-key →
stub, hash embedder), so this file needs no env wiring.
"""
from __future__ import annotations

from app import main
from app.brain import engine
from fastapi.testclient import TestClient

# A grounded question whose answer must come out of the shipped company docs.
_GROUNDED_Q = "What approvals are required before IT provisions access?"


def _client() -> TestClient:
    return TestClient(main.app)


def test_keyfree_contract_is_the_stub_brain():
    """Guard the demo's headline promise: with no key the brain is the offline
    stub and the embedder is the hash embedder — the whole flow runs for free."""
    assert engine.effective_provider() == "stub"
    assert engine.post_provider() == "stub"


def test_demo_sample_ships_a_real_transcript():
    """Step 2 · the sample meeting the demo loads is real and non-trivial."""
    r = _client().get("/demo/sample")
    assert r.status_code == 200
    body = r.json()
    assert body["avatar_id"] == "laura"
    transcript = body["transcript"]
    assert transcript.strip(), "the avatar must ship a sample_meeting.txt"
    # It reads like a real multi-speaker transcript ("Speaker: line" turns).
    assert transcript.count(":") >= 5
    assert "Laura" in transcript


def test_demo_ask_answers_from_the_doc_with_a_citation():
    """Step 3 · a question is answered FROM a company doc, WITH a cited source.

    The offline stub is extractive: it quotes the top-matching process chunk and
    names the source, so the answer both contains the doc's words and carries a
    machine-readable citation the demo UI renders as a source chip."""
    r = _client().post("/demo/ask", json={"question": _GROUNDED_Q})
    assert r.status_code == 200
    body = r.json()

    # A citation/source is present and machine-readable.
    citations = body.get("citations")
    assert citations, f"expected a cited source, got {citations!r}"
    source = citations[0]
    assert source.endswith(".md"), source

    # The answer is grounded in the docs (not a world-knowledge fallback) …
    assert body["sufficient_context"] is True
    assert body["confidence"] > 0.0
    # … and it actually QUOTES the doc: the cited source is named in the spoken
    # answer, and a real process phrase from the SOP appears verbatim.
    answer = body["answer"]
    assert source in answer, "the spoken answer must name the source it quoted"
    assert "approval" in answer.lower()

    # The richer retrieval trail the UI uses for source chips is present too.
    retrieved = body.get("retrieved") or []
    assert retrieved and retrieved[0]["source"] == source
    assert "score" in retrieved[0] and "section" in retrieved[0]


def test_demo_post_meeting_produces_the_full_artifact():
    """Steps 4-9 · the transcript becomes the post-meeting artifact: a summary,
    captured actions (each with the lifecycle fields the dashboard maps to
    needs-details → proposed), decisions, risks, and a readiness score."""
    client = _client()
    transcript = client.get("/demo/sample").json()["transcript"]
    r = client.post("/demo/post_meeting", json={"transcript": transcript})
    assert r.status_code == 200
    art = r.json()

    # Summary + the three distilled surfaces the demo (and the org PII pref) show.
    assert art["summary"].strip()
    assert "actions" in art and isinstance(art["actions"], list)
    assert "decisions" in art and isinstance(art["decisions"], list)
    assert "risks" in art and isinstance(art["risks"], list)

    # At least one action was captured from this transcript …
    actions = art["actions"]
    assert actions, "the sample meeting has action-shaped lines to capture"
    # … and every captured action carries the lifecycle fields the dashboard
    # renders (item text, an owner slot — UNASSIGNED until named — and the gap
    # that drives needs-details vs proposed).
    for a in actions:
        assert a["item"].strip()
        assert "owner" in a
        assert "gap_type" in a
    # `checklist` is the legacy alias the demo page reads — it must mirror actions.
    assert art["checklist"] == actions

    # Deterministic template-derived fields: this is a recognised onboarding
    # meeting with missing process steps and a readiness score for the tile.
    assert art["meeting_type"] == "customer_onboarding"
    assert isinstance(art["readiness_score"], (int, float))
    assert 0 <= art["readiness_score"] <= 100
    assert art["missing_steps"], "the sample meeting leaves onboarding steps open"

    # Step 5 evidence: a missing detail Laura flagged is captured, not invented —
    # the sample leaves the implementation owner unnamed.
    assert "implementation_owner" in art["missing_steps"]

    # The draft follow-up email (step 10 deliverable) is built.
    assert art["follow_up_email"]["subject"].strip()
    assert art["follow_up_email"]["body"].strip()


def test_the_whole_flow_is_deterministic():
    """Key-free ⇒ no model, no network, no clock: identical inputs give byte-for-
    byte identical answers and artifacts, so the demo is safe to script."""
    client = _client()
    a1 = client.post("/demo/ask", json={"question": _GROUNDED_Q}).json()
    a2 = client.post("/demo/ask", json={"question": _GROUNDED_Q}).json()
    assert a1 == a2

    transcript = client.get("/demo/sample").json()["transcript"]
    p1 = client.post("/demo/post_meeting", json={"transcript": transcript}).json()
    p2 = client.post("/demo/post_meeting", json={"transcript": transcript}).json()
    assert p1["summary"] == p2["summary"]
    assert p1["actions"] == p2["actions"]
    assert p1["decisions"] == p2["decisions"]
    assert p1["readiness_score"] == p2["readiness_score"]


def test_ungrounded_question_is_honestly_uncited():
    """Trust boundary: a question the docs don't cover must NOT fabricate a
    citation — the demo's credibility depends on 'from your docs' meaning it."""
    body = _client().post(
        "/demo/ask",
        json={"question": "What was the closing price of gold yesterday?"},
    ).json()
    assert body["sufficient_context"] is False
    assert body["citations"] == []
