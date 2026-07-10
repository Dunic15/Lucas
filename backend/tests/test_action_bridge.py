"""The action bridge: queue_action live tool -> artifact/ledger + action.requested.

Platform feature (every avatar gets it): during a meeting, "can you send the
recap?" is CAPTURED in-memory on the live session by tools.queue_action —
instant, zero I/O on the live path — then folded into the artifact's actions[]
(and the ledger) at finalize. Orchestrated sessions additionally fire a
best-effort action.requested webhook per capture, so the approval card is
ready before the meeting ends.

Key-free like the rest of the suite: recall/anam are monkeypatched, callbacks
hit local recorders, the brain stays stub.
"""
from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, store, tools
from app.cedric import callback as cedric_callback


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    # Reload store (fresh sqlite at the tmp path) and ledger (recreates its
    # tables in that fresh DB). Reload mutates the module objects in place, so
    # main's `from . import store, ledger` references follow automatically.
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    created: list[dict] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura"):
        created.append({"meeting_url": meeting_url, "bot_name": bot_name})
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda conv_id: None
    )
    return created


START_BODY = {
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "avatar_id": "cedric",
    "callback_url": "https://cedric.example/api/meet/callback",
    "external_ref": {"team": "T1", "meet_session_id": "ms_1"},
    "context": {
        "meeting": {"title": "Q3 sync"},
        "brief_markdown": "## Why this meeting\nDiscuss Q3.",
    },
}


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _wait_until(cond, timeout: float = 2.0) -> bool:
    """Poll for an async/off-thread side effect (the webhook is fire-and-forget
    by design, so tests wait for the recorder instead of the return value)."""
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    return cond()


# ── (a) dispatch: confirmation + capture on the session ────────────────


def test_queue_action_dispatch_captures_on_session(client):
    session = store.create("bot_a", "https://meet.example/a", "cedric")

    out = tools.dispatch(
        "queue_action",
        {"action": "Send the recap to Marco", "owner": "Ben", "due": "Friday"},
        session=session,
    )
    # Spoken confirmation promises follow-up, never execution.
    assert "queue" in out.lower() and "approval" in out.lower()
    assert not out.lower().startswith("error")
    assert len(session.queued_actions) == 1
    a0 = session.queued_actions[0]
    assert {k: a0[k] for k in ("action", "owner", "due")} == {
        "action": "Send the recap to Marco", "owner": "Ben", "due": "Friday"
    }
    # A stable id is assigned at capture — it rides action.requested and the
    # artifact so the orchestrator correlates the two.
    assert a0["action_id"] and isinstance(a0["action_id"], str)

    # dispatch_for binds the session with the (name, args) signature the LLM
    # tool loop expects.
    bound = tools.dispatch_for(session)
    bound("queue_action", {"action": "Book a follow-up"})
    assert len(session.queued_actions) == 2
    a1 = session.queued_actions[1]
    assert {k: a1[k] for k in ("action", "owner", "due")} == {
        "action": "Book a follow-up", "owner": "", "due": ""
    }
    assert a1["action_id"] and a1["action_id"] != a0["action_id"]  # distinct per action

    # Validation: an empty action is rejected and captures nothing.
    err = tools.dispatch("queue_action", {"action": "   "}, session=session)
    assert err.startswith("error")
    assert len(session.queued_actions) == 2

    # The session seam is additive: plain tools are untouched, with or
    # without a session threaded in.
    assert tools.dispatch("calculator", {"expression": "2+2"}) == "4"
    assert bound("calculator", {"expression": "3*3"}) == "9"


def test_queue_action_without_session_is_honest():
    out = tools.dispatch("queue_action", {"action": "Send the recap"})
    assert "nothing was queued" in out.lower()


def test_queue_action_registered_in_tool_specs():
    names = [t["function"]["name"] for t in tools.TOOL_SPECS]
    assert "queue_action" in names  # platform-level: every avatar gets it
    spec = next(t for t in tools.TOOL_SPECS if t["function"]["name"] == "queue_action")
    assert spec["function"]["parameters"]["required"] == ["action"]


# ── (b) finalize: captured items merge + dedupe into actions[] ─────────


def test_finalize_merges_and_dedupes_actions(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)
    monkeypatch.setattr(cedric_callback, "send_ended", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    # The stub summarizer extracts this line as an action item too ("need").
    session.add_utterance("Ben", "We need to send the recap to Marco")

    tools.dispatch(
        "queue_action",
        # Different case + punctuation than the transcript line: dedupe is on
        # NORMALIZED item text.
        {"action": "we need to send the recap to marco.", "owner": "Ben", "due": "Friday"},
        session=session,
    )
    tools.dispatch(
        "queue_action",
        {"action": "Book a follow-up with the design team"},
        session=session,
    )

    resp = client.post(f"/sessions/{bot_id}/end")
    assert resp.status_code == 200
    actions = resp.json()["actions"]

    # The summarizer's duplicate collapsed into the live capture: exactly one.
    recaps = [a for a in actions if _norm(a["item"]) == "we need to send the recap to marco"]
    assert len(recaps) == 1
    assert recaps[0]["requested_live"] is True  # the live capture won
    assert recaps[0]["owner"] == "Ben"
    assert recaps[0]["deadline"] == "Friday"

    followups = [a for a in actions if _norm(a["item"]) == "book a follow up with the design team"]
    assert len(followups) == 1
    assert followups[0]["owner"] == "UNASSIGNED"
    assert followups[0]["gap_type"] == "owner"  # nobody was named


# ── (c) orchestrated: action.requested fires once per capture ──────────


def test_orchestrated_capture_fires_action_requested(client, recall_stubbed, monkeypatch):
    delivered: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback,
        "send_action_requested",
        lambda integration, bot_id, item: delivered.append((integration, bot_id, item))
        or True,
    )

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    tools.dispatch(
        "queue_action",
        {"action": "Send the recap to Marco", "owner": "Ben"},
        session=session,
    )
    tools.dispatch(
        "queue_action", {"action": "Book a follow-up", "due": "Friday"}, session=session
    )

    assert _wait_until(lambda: len(delivered) >= 2)
    time.sleep(0.05)
    assert len(delivered) == 2  # exactly once per capture

    integration, delivered_bot, item = delivered[0]
    assert delivered_bot == bot_id
    assert integration["external_ref"] == {"team": "T1", "meet_session_id": "ms_1"}
    # PII rule: only the distilled action/owner/due cross (+ the non-PII
    # correlation action_id) — never transcript content.
    assert {k: item[k] for k in ("action", "owner", "due")} == {
        "action": "Send the recap to Marco", "owner": "Ben", "due": ""
    }
    assert item["action_id"]  # the live event carries the id for correlation
    d1 = delivered[1][2]
    assert {k: d1[k] for k in ("action", "owner", "due")} == {
        "action": "Book a follow-up", "owner": "", "due": "Friday"
    }
    assert d1["action_id"] and d1["action_id"] != item["action_id"]


def test_action_requested_payload_and_external_ref_echo(monkeypatch):
    posts: list[tuple] = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, payload):
        posts.append((url, payload))
        return FakeResponse()

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    ok = cedric_callback.send_action_requested(
        {"callback_url": "https://cedric.example/cb", "external_ref": {"team": "T1"}},
        "bot_7",
        {"action": "Send the recap", "owner": "Ben", "due": "Friday"},
    )
    assert ok is True
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://cedric.example/cb"
    assert payload["event"] == "action.requested"
    assert payload["bot_id"] == "bot_7"
    assert payload["external_ref"] == {"team": "T1"}  # echoed for correlation
    assert payload["action"] == "Send the recap"
    assert payload["owner"] == "Ben"
    assert payload["due"] == "Friday"
    assert payload["at"]


def test_action_requested_is_single_attempt_best_effort(monkeypatch):
    attempts: list = []

    def fake_post(url, payload):
        attempts.append(payload)
        raise RuntimeError("down")

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    ok = cedric_callback.send_action_requested(
        {"callback_url": "https://cedric.example/cb"}, "bot_1", {"action": "x"}
    )
    assert ok is False
    assert len(attempts) == 1  # same discipline as session.status: no retries
    # No callback_url -> no delivery at all.
    assert cedric_callback.send_action_requested(None, "b", {"action": "x"}) is False
    assert cedric_callback.send_action_requested({}, "b", {"action": "x"}) is False


def test_action_requested_surface_rejection_is_logged(monkeypatch, capsys):
    """Live-diagnosis (2026-07-10): a non-2xx from the surface used to be
    swallowed (returned False, no log) — so 'Cedric did nothing' was
    invisible. It must now log the HTTP status, PII-safe (id + code only)."""

    class Rejected:
        status_code = 404

    monkeypatch.setattr(cedric_callback, "_post", lambda url, payload: Rejected())
    ok = cedric_callback.send_action_requested(
        {"callback_url": "https://cedric.example/cb"},
        "bot_9",
        {"action_id": "abc123", "action": "Send the recap"},
    )
    assert ok is False
    out = capsys.readouterr().out
    assert "REJECTED by surface" in out and "HTTP 404" in out
    assert "abc123" in out and "Send the recap" not in out  # id yes, content no


def test_non_orchestrated_capture_logs_reason(monkeypatch, capsys):
    """A captured action on a session with no callback_url must say WHY nothing
    reached the surface — the diagnostic that tells a mis-summoned session
    (Model B / plain) apart from a real send."""
    from app import cedric

    session = store.create("bot_noorch", "https://meet.example/x", "cedric")
    cedric.notify_action_requested(session, "bot_noorch", {"action_id": "z9"})
    out = capsys.readouterr().out
    assert "not orchestrated" in out.lower() or "no callback_url" in out.lower()
    assert "z9" in out
    store.remove("bot_noorch")


# ── (d) plain session: no webhook, but artifact + ledger still get it ──


def test_plain_session_no_webhook_but_artifact_and_ledger(
    client, recall_stubbed, monkeypatch
):
    fired: list = []
    monkeypatch.setattr(
        cedric_callback, "send_action_requested", lambda *a: fired.append(a) or True
    )

    session = store.create("bot_plain", "https://meet.example/plain", "laura")
    session.add_utterance("Priya", "Let's kick off the quarterly review.")
    out = tools.dispatch(
        "queue_action",
        {"action": "Email the pricing deck to Acme", "owner": "Priya"},
        session=session,
    )
    assert "queue" in out.lower()

    time.sleep(0.1)
    assert fired == []  # non-orchestrated: action.requested never fires

    resp = client.post("/sessions/bot_plain/end")
    assert resp.status_code == 200
    actions = resp.json()["actions"]
    assert any(a.get("item") == "Email the pricing deck to Acme" for a in actions)

    # …and the ledger recorded it as an open action (same path as any meeting).
    key = ledger.meeting_key("https://meet.example/plain")
    items = ledger.items(key)
    assert any(
        i["kind"] == "action"
        and i["item"] == "Email the pricing deck to Acme"
        and i["owner"] == "Priya"
        and i["status"] == "open"
        for i in items
    )


# ── live route: the deterministic capture branch in the meeting loop ────
# These exercise the REAL webhook path (POST /webhooks/recall transcript.data
# → detect_wake → capture branch), not just the tool layer.


def _transcript_payload(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": 1},
            },
        },
    }


@pytest.fixture
def spoken(monkeypatch):
    """Record every line the avatar would speak instead of pushing to a ws."""
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    return lines


def _post_final(client, bot_id: str, speaker: str, text: str) -> dict:
    resp = client.post(
        "/webhooks/recall", json=_transcript_payload(bot_id, speaker, text)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_live_route_async_ask_captures_and_confirms(
    client, recall_stubbed, spoken, monkeypatch
):
    fired: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback,
        "send_action_requested",
        lambda integration, bot_id, item: fired.append((integration, bot_id, item))
        or True,
    )

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _post_final(
        client, bot_id, "Ben", "Cedric, please schedule a follow-up with Marco on Friday"
    )
    assert body.get("action_capture") is True

    session = store.get(bot_id)
    assert len(session.queued_actions) == 1
    # detect_wake stripped the name; the ask itself is the queued action.
    assert "schedule a follow-up with marco" in session.queued_actions[0]["action"].lower()
    assert "cedric" not in session.queued_actions[0]["action"].lower()

    # The spoken reply is the fixed confirmation (instant, TTS-prewarmed pool).
    queue_pool = main_module._QUEUE_LINES + main_module._QUEUE_LINES_IT
    assert spoken and spoken[-1] in queue_pool

    # Orchestrated session → action.requested fired exactly once, ref echoed.
    assert _wait_until(lambda: len(fired) == 1)
    integration, fired_bot, item = fired[0]
    assert fired_bot == bot_id
    assert integration["external_ref"] == {"team": "T1", "meet_session_id": "ms_1"}


def test_live_route_capture_continuation_extends_item(
    client, recall_stubbed, spoken, monkeypatch
):
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]

    # First final captures; the same speaker's immediate follow-up (no wake
    # word) extends the SAME item rather than becoming a new utterance.
    _post_final(client, bot_id, "Ben", "Cedric, please send the recap")
    body2 = _post_final(client, bot_id, "Ben", "to the whole team by Friday")
    assert body2.get("capture_extended") is True

    session = store.get(bot_id)
    assert len(session.queued_actions) == 1  # still one action, just longer
    action = session.queued_actions[0]["action"].lower()
    assert "send the recap" in action and "by friday" in action

    # A DIFFERENT speaker's next line is NOT swallowed into the capture.
    session.last_capture = (session.queued_actions[0], "Ben", time.time())
    body3 = _post_final(client, bot_id, "Marco", "I think Tuesday is better")
    assert body3.get("capture_extended") is None


def test_live_route_content_question_is_not_captured(
    client, recall_stubbed, spoken, monkeypatch
):
    fired: list = []
    monkeypatch.setattr(
        cedric_callback, "send_action_requested", lambda *a: fired.append(a) or True
    )

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _post_final(
        client, bot_id, "Ben", "Cedric, can you check if the numbers add up?"
    )
    assert body.get("action_capture") is None  # fell through to the answer path

    session = store.get(bot_id)
    assert getattr(session, "queued_actions", []) == []
    time.sleep(0.1)
    assert fired == []
    queue_pool = main_module._QUEUE_LINES + main_module._QUEUE_LINES_IT
    assert all(line not in queue_pool for line in spoken)


def test_live_route_search_intent_is_not_captured(
    client, recall_stubbed, spoken, monkeypatch
):
    from app.config import settings

    # wants_web_search is gated on a provider key (key-free = never search);
    # simulate production so the search-vs-capture precedence is exercised —
    # and stub the streamed answer so no real provider call happens.
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    def fake_stream(*a, **k):
        yield "Here's the latest."

    monkeypatch.setattr(main_module, "answer_question_stream", fake_stream)
    fired: list = []
    monkeypatch.setattr(
        cedric_callback, "send_action_requested", lambda *a: fired.append(a) or True
    )

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _post_final(
        client, bot_id, "Ben", "Cedric, can you send me the latest news on OpenAI?"
    )

    session = store.get(bot_id)
    assert getattr(session, "queued_actions", []) == []
    time.sleep(0.1)
    assert fired == []  # search intent keeps its announced streamed answer


# ── trigger narrowness: the reviewer's false-positive phrases stay out ──


def test_wants_action_capture_is_narrow():
    from app.brain import wants_action_capture

    for phrase in [
        "can you check if the numbers add up?",
        "can you check if that's in the docs",
        "could you check what our uptime SLA is?",
        "puoi controllare se i numeri tornano?",
        "can you share your thoughts on this?",
        "can you remind me what we decided last week?",
        "can you open the deck?",
        "would you go through the numbers",
    ]:
        assert not wants_action_capture(phrase), f"must NOT capture: {phrase!r}"

    for phrase in [
        "can you schedule a follow-up with Marco for Friday?",
        "please send the recap to the team",
        "could you book a meeting with Elena next week?",
        "can you draft an email to the client?",
        "will you remind me to update the doc tomorrow?",
        "can you open a ticket for the login bug?",
        "please add it to the calendar",
        "puoi mandare il recap a Elena?",
        "mi fissi una call per giovedì?",
        "ricordami di aggiornare il documento",
    ]:
        assert wants_action_capture(phrase), f"must capture: {phrase!r}"


def test_wants_action_capture_bare_imperatives():
    """The 0-actions bug: users speak BARE IMPERATIVES ("schedule…", "send…",
    "post…") with no "can you/please" carrier. The capture regex must catch the
    verb leading the (wake-stripped) ask, plus messaging verbs (post/ping/dm/
    message) the old list lacked — without capturing plain statements."""
    from app.brain import wants_action_capture

    for phrase in [
        "schedule a follow-up with Marco",
        "send Priya an email",
        "post to Slack",
        "post the recap to the channel",
        "ping Marco about the deck",
        "dm Priya the notes",
        "message the team the update",
        "ok, schedule a follow-up with Marco next week",
        "so send the recap to the team",
        "also add Marco to the calendar",
    ]:
        assert wants_action_capture(phrase), f"must capture: {phrase!r}"

    for phrase in [
        "we should send the recap at some point",  # statement, not imperative
        "I'll email him later today",
        "post-meeting we can review the deck",  # 'post-meeting' is not 'post'
        "the message from the client was clear",  # 'message' as a noun
        "so what is the status of the project",
    ]:
        assert not wants_action_capture(phrase), f"must NOT capture: {phrase!r}"


def test_live_route_bare_imperative_captures(client, recall_stubbed, spoken, monkeypatch):
    """End-to-end on the real webhook path: a bare-imperative ask with no
    carrier is captured on the session (this is exactly what failed live)."""
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _post_final(client, bot_id, "Ben", "Cedric, schedule a follow-up with Marco on Friday")
    assert body.get("action_capture") is True

    session = store.get(bot_id)
    assert len(session.queued_actions) == 1
    assert "schedule a follow-up with marco" in session.queued_actions[0]["action"].lower()


# ── stable action_id: correlate the live event to the final artifact ───


def test_action_id_correlates_live_event_and_artifact(client, recall_stubbed, spoken, monkeypatch):
    """The SAME action_id rides the live action.requested AND appears on that
    action in the session.ended artifact — so Cedric dedupes the live approval
    card against the final action on the id, not on text (the continuation
    window can extend the text after the live event already fired)."""
    live_ids: list = []
    monkeypatch.setattr(
        cedric_callback,
        "send_action_requested",
        lambda integration, bot_id, item: live_ids.append(item.get("action_id")) or True,
    )
    monkeypatch.setattr(cedric_callback, "send_ended", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _post_final(client, bot_id, "Ben", "Cedric, schedule a follow-up with Marco on Friday")
    assert _wait_until(lambda: len(live_ids) == 1)
    live_id = live_ids[0]
    assert live_id  # the live event carried a real id

    actions = client.post(f"/sessions/{bot_id}/end").json()["actions"]
    match = [a for a in actions if a.get("action_id") == live_id]
    assert len(match) == 1  # correlated across channels by id, exactly once
    assert "schedule a follow-up with marco" in match[0]["item"].lower()
    assert match[0]["requested_live"] is True


def test_every_finalized_action_is_id_stamped(client, recall_stubbed, monkeypatch):
    """Even a summarizer-only action (no live queue_action) gets a stable
    action_id at finalize, so every action Cedric receives is addressable."""
    monkeypatch.setattr(cedric_callback, "send_ended", lambda *a: True)
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    session.add_utterance("Ben", "We need to send the rollout doc to Marco by Friday")

    actions = client.post(f"/sessions/{bot_id}/end").json()["actions"]
    assert actions  # the stub extracts the "need to…" line as an action
    assert all(a.get("action_id") for a in actions)  # every one addressable


def test_wants_action_capture_italian_bare_imperatives():
    """Italian bare imperatives ("manda una mail…", "prenota una call…") must be
    captured live at parity with English — the workflow found they were dropped
    (only periphrastic "puoi mandare…" matched). Anchored, so mid-sentence
    indicatives ("dovremmo mandare…") stay out."""
    from app.brain import wants_action_capture

    for phrase in [
        "manda una mail a Priya con il riassunto",
        "allora manda il documento",
        "prenota una call con Marco venerdì",
        "invia il recap a Elena",
        "fissa una riunione giovedì",
        "mandami il file",
        "ricordami di chiamare Marco",
        # calendar asks as they actually sound live (2026-07-10 test:
        # "schedula il meeting" routed to the slow answer path — Cedric
        # "thought hard" instead of instantly noting it down)
        "schedula il meeting con Marco per domani",
        "puoi schedulare una call con Ben",
        "mi scheduli la review di venerdì?",
        "crea un meeting per lunedì alle 10",
        "aggiungi la demo al calendario",
        "metti in calendario il follow-up",
    ]:
        assert wants_action_capture(phrase), f"must capture: {phrase!r}"

    for phrase in [
        "dovremmo mandare una mail",       # suggestion, not an imperative
        "la mail la manda Priya domani",   # 'manda' mid-sentence indicative
        "controlla se i numeri tornano",   # content query, not an action
        "abbiamo schedulato il meeting ieri",  # past indicative, not an ask
        "il meeting è schedulato per domani",
    ]:
        assert not wants_action_capture(phrase), f"must NOT capture: {phrase!r}"


def test_merge_dedupes_summarizer_rephrase_of_a_live_action():
    """The summarizer re-extracts a live-captured action, rephrased (deadline
    split into its own field). It must NOT mint a second action_id — the live
    entry wins and absorbs the structured deadline/owner. Exact-text dedup missed
    this (two action_ids for one request); semantic dedup catches it. (#84)"""
    import app.main as main_module

    out = main_module._merge_action_items(
        [{"action_id": "live1", "action": "send the rollout doc to Marco by Friday",
          "owner": "", "due": ""}],
        [{"item": "Send the rollout doc to Marco", "owner": "Ben",
          "deadline": "Friday", "gap_type": "none"}],
    )
    assert len(out) == 1  # one request → one action, not two
    a = out[0]
    assert a["requested_live"] is True and a["action_id"] == "live1"  # live wins
    assert a["deadline"] == "Friday" and a["owner"] == "Ben"  # folded from summarizer

    # Genuinely distinct actions are NOT merged (guard against over-dedup).
    out2 = main_module._merge_action_items(
        [{"action_id": "l", "action": "send the deck to Priya", "due": ""}],
        [{"item": "email Marco the contract"}],
    )
    assert len(out2) == 2


# ── (e) the wire artifact stays transcript-free ────────────────────────


def test_wire_artifact_still_transcript_free(client, recall_stubbed, monkeypatch):
    delivered: list[dict] = []
    monkeypatch.setattr(
        cedric_callback,
        "send_ended",
        lambda integration, bot_id, artifact: delivered.append(artifact) or True,
    )
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    session.add_utterance("Ben", "Cedric, can you send the recap to Marco?")
    tools.dispatch(
        "queue_action", {"action": "Send the recap to Marco"}, session=session
    )

    resp = client.post(f"/sessions/{bot_id}/end")
    wire = resp.json()
    assert "transcript" not in wire
    assert wire["artifact_version"] == 1
    assert any(a.get("requested_live") for a in wire["actions"])

    assert _wait_until(lambda: len(delivered) == 1)
    assert "transcript" not in delivered[0]
    assert any(a.get("requested_live") for a in delivered[0]["actions"])

    # The full artifact (transcript included) still lives in the LOCAL store
    # only — the PII boundary is the orchestrator API, not disk.
    assert "transcript" in store.get_artifact(bot_id)
