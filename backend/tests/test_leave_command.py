"""Voice dismissal ("Laura, you can leave"): detection + webhook flow. No keys."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import detect_leave_command, detect_wake  # noqa: E402


# ── detection: commands that MUST trigger ──

LEAVE_ASKS = [
    "you can leave now",
    "you can leave",
    "You may leave the meeting",
    "you should leave now",
    "you are free to go",
    "you can go now",
    "leave the meeting",
    "please leave the call",
    "leave now",
    "leave",
    "drop off",
    "hop off the call",
    "hang up",
    "disconnect",
    "you can leave, thanks for the help",
    "thanks, you can leave now",
    "bye",
    "goodbye",
    "bye bye",
    "ciao ciao",
    "arrivederci",
    "see you later",
    # Italian dismissals
    "puoi andare",
    "puoi uscire dalla riunione",
    "puoi lasciarci, grazie",
    "esci pure",
    "vai pure",
    "vai via",
    "abbandona la call",
    "sei libera di andare",
    "non ci servi più",
    # go-out family
    "go out of the meeting",
    "get out",
    "go away",
    "you can log off now",
    "you can sign off",
    "you can hang up now",
    # ASR/common "out" variants of the off-family (live miss candidates)
    "you can drop out",
    "you can log out now",
    "sign out of the meeting",
    "drop out of the call",
    "you are free to drop out",
    # non-native / ASR-noisy prepositions
    "go out from the meeting",
    "go out the meeting",
    "you can go out of the meeting",
    "exit from the call",
    # the polite QUESTION form ("Laura, can you leave the meeting?")
    "can you leave the meeting",
    "can you leave the meeting?",
    "could you please leave the call",
    "would you leave the meeting now",
    "can you go out of the meeting",
    "will you hang up now",
    # Italian round 2
    "lascia la riunione",
    "lasciaci pure la call",
    "vai fuori dalla riunione",
    "potresti uscire dalla call",
    "puoi andartene",
    "te ne puoi andare",
]


def test_leave_commands_detected():
    for ask in LEAVE_ASKS:
        assert detect_leave_command(ask), f"should trigger: {ask!r}"


# ── detection: normal meeting talk that must NEVER kill the bot ──

NOT_LEAVE_ASKS = [
    # Italian: "ciao" alone is a GREETING ("Laura, ciao!"), never a dismissal;
    # "puoi andare avanti" means "go ahead", not "leave".
    "ciao",
    "ciao come stai",
    "puoi andare avanti",
    "puoi andare più veloce",
    "non andare via",
    "prima di uscire fai il riepilogo",
    "what did we leave open last time",
    "leave the pricing discussion for next week",
    "leave it with me",
    "leave that aside for now",
    "leave room for questions at the end",
    "you can leave time for Q&A",
    "you can go deeper on that",
    "you can go ahead",
    "you can go to the next slide",
    "you can go",
    "your turn, you can go",
    "we asked you a question, you can go",
    "before you leave the meeting can you summarize",
    "don't leave yet",
    "why did you leave that out",
    "when you leave the office tonight",
    "she took a leave of absence",
    "can we leave the DPA point to legal",
    "goodbye emails should go out on Friday",
    "what's the process",
    "",
    # question-form near misses: a topic after the verb is never a dismissal
    "can you leave the pricing for next week",
    "can you leave time for Q&A",
    "could you leave room for questions",
    "can you go out and check the numbers",
    "would you go through the numbers",
    "can you go over the agenda",
    "will you leave the company retreat planning to Sam",
    # off/out-family near misses: business idioms, negations, unrelated words
    "let us sign off on the budget tomorrow",
    "do not drop out of the program",
    "the dropout rate is high",
    # Italian near misses
    "lascia stare",
    "lascia perdere il punto due",
    "puoi andare al prossimo punto",
    "potresti andare più veloce",
]


def test_normal_talk_not_detected():
    for ask in NOT_LEAVE_ASKS:
        assert not detect_leave_command(ask), f"must NOT trigger: {ask!r}"


def test_end_to_end_with_wake_strip():
    from app import avatars

    avatar = avatars.load("laura")
    for utterance, expected in [
        ("Laura, you can leave now", True),
        ("Hey Laura, please leave the meeting", True),
        ("Bye Laura!", True),
        ("Laura, what did we leave open?", False),
        ("Laura, leave the pricing for next week", False),
    ]:
        called, question = detect_wake(avatar, utterance)
        assert called, f"wake word should match: {utterance!r}"
        assert detect_leave_command(question) is expected, utterance


# ── webhook flow: goodbye → leave_call → artifact → session removed ──


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


def _stub_vendors(monkeypatch, tmp_path, calls: dict) -> None:
    """Keep the real webhook flow but stub everything that talks to the world."""
    monkeypatch.setattr(settings, "leave_grace_seconds", 0.0)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()  # fresh DB file needs the schema
    monkeypatch.setattr(
        main.recall_client,
        "leave_call",
        lambda bid: calls.__setitem__("leave", calls["leave"] + 1),
    )
    monkeypatch.setattr(
        main, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)


def _make_session(bot_id: str) -> store.Session:
    """Create through the real store (tmp DB is already patched in)."""
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""  # skip the lazy ledger carryover load
    return s


def _post_line(bot_id: str, text: str) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            import json

            return json.dumps(_transcript_payload(bot_id, "Duccio", text)).encode()

    import json

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def _run_webhook(monkeypatch, tmp_path, bot_id: str, text: str) -> tuple[dict, dict]:
    """Post one transcript line through the real webhook handler, vendors stubbed."""
    calls = {"leave": 0, "spoken": []}
    _stub_vendors(monkeypatch, tmp_path, calls)
    _make_session(bot_id)

    async def fake_speak(session, line, citations=None, **kw):
        calls["spoken"].append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    return _post_line(bot_id, text), calls


def test_webhook_leave_command_ends_session(monkeypatch, tmp_path):
    bot_id = "leave-bot-1"
    body, calls = _run_webhook(monkeypatch, tmp_path, bot_id, "Laura you can leave now")
    assert body.get("left") is True
    assert calls["leave"] == 1, "bot must leave the call (meter stops)"
    assert calls["spoken"], "she should say goodbye before leaving"
    assert store.get(bot_id) is None, "session must be finalized/removed"
    assert store.get_artifact(bot_id) is not None, "post-meeting artifact still built"


def test_webhook_leave_talk_does_not_end_session(monkeypatch, tmp_path):
    bot_id = "leave-bot-2"

    def no_answer(*a, **k):
        yield from ()

    monkeypatch.setattr(main, "answer_question_stream", no_answer)
    body, calls = _run_webhook(
        monkeypatch, tmp_path, bot_id, "Laura what did we leave open last time"
    )
    assert body.get("left") is None
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None, "session must survive"
    store.remove(bot_id)


def test_webhook_leave_survives_tts_failure(monkeypatch, tmp_path):
    """Meter safety: a goodbye/TTS crash must never keep the bot in the call."""
    bot_id = "leave-bot-3"
    calls = {"leave": 0, "spoken": []}
    _stub_vendors(monkeypatch, tmp_path, calls)
    _make_session(bot_id)

    async def broken_speak(*a, **k):
        raise RuntimeError("tts down")

    monkeypatch.setattr(main, "_make_avatar_speak", broken_speak)
    body = _post_line(bot_id, "Laura please leave the meeting")
    assert body.get("left") is True
    assert calls["leave"] == 1
    assert store.get(bot_id) is None


def test_leave_disabled_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "leave_on_command", False)
    bot_id = "leave-bot-4"

    def no_answer(*a, **k):
        yield from ()

    monkeypatch.setattr(main, "answer_question_stream", no_answer)
    body, calls = _run_webhook(monkeypatch, tmp_path, bot_id, "Laura you can leave now")
    assert body.get("left") is None
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None
    store.remove(bot_id)


# ── Cedric soft-C name: ASR-spelling wake + split-final dismissal ──


def test_cedric_asr_spellings_wake_and_leave():
    """The soft-C name is transcribed "Sedric"/"Cedrick"/"Kedric"; each spelling
    must still wake Cedric AND carry the leave command through (before this fix
    "Cedric, you can leave" as "Sedric, …" silently never fired)."""
    from app import avatars

    cedric = avatars.load("cedric")
    for utterance in (
        "Sedric, you can leave",
        "Sedrick, you can leave now",
        "Cedrick, you can leave",
        "Kedric, you can leave",  # not an alias — resolves via the fuzzy path
    ):
        called, question = detect_wake(cedric, utterance)
        assert called, f"Cedric should wake on ASR spelling: {utterance!r}"
        assert detect_leave_command(question), f"leave should fire: {utterance!r}"


def _post_line_as(bot_id: str, text: str, speaker: str) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            import json

            return json.dumps(_transcript_payload(bot_id, speaker, text)).encode()

    import json

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def _stub_cedric_webhook(monkeypatch, tmp_path, bot_id: str) -> dict:
    """A Cedric session with vendors + speak + answer stubbed; returns the call
    tracker. Lets a test post several finals to the SAME live session."""
    calls = {"leave": 0, "spoken": []}
    _stub_vendors(monkeypatch, tmp_path, calls)
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "cedric")
    s.memory_brief = ""

    async def fake_speak(session, line, citations=None, **kw):
        calls["spoken"].append(line)
        return True

    def no_answer(*a, **k):
        yield from ()

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    monkeypatch.setattr(main, "answer_question_stream", no_answer)
    return calls


def test_webhook_split_leave_across_finals(monkeypatch, tmp_path):
    """"Cedric." then "you can leave" arrive as TWO ASR finals — neither alone
    fires the dismissal (the reported bug: a reconcile poll ended the meeting
    late). Same speaker within the window must still end it now."""
    bot_id = "leave-split-1"
    calls = _stub_cedric_webhook(monkeypatch, tmp_path, bot_id)

    b1 = _post_line_as(bot_id, "Cedric.", "Duccio")
    assert b1.get("left") is None, "the bare name alone must NOT end the meeting"
    assert store.get(bot_id) is not None

    b2 = _post_line_as(bot_id, "you can leave", "Duccio")
    assert b2.get("left") is True, "split dismissal across finals must fire"
    assert calls["leave"] == 1, "bot must leave the call (meter stops)"
    assert store.get(bot_id) is None, "session must be finalized/removed"


def test_webhook_split_leave_requires_same_speaker(monkeypatch, tmp_path):
    """Meter safety: a stray "you can leave" from a DIFFERENT speaker right after
    someone named the avatar must NEVER end the meeting early."""
    bot_id = "leave-split-2"
    calls = _stub_cedric_webhook(monkeypatch, tmp_path, bot_id)

    _post_line_as(bot_id, "Cedric.", "Duccio")
    b2 = _post_line_as(bot_id, "you can leave", "Ben")
    assert b2.get("left") is None
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None
    store.remove(bot_id)


def test_webhook_split_leave_only_on_leave_followup(monkeypatch, tmp_path):
    """Meter safety: addressing the avatar then just talking must not end the
    meeting — the follow-up itself has to be a leave command."""
    bot_id = "leave-split-3"
    calls = _stub_cedric_webhook(monkeypatch, tmp_path, bot_id)

    _post_line_as(bot_id, "Cedric.", "Duccio")
    b2 = _post_line_as(bot_id, "let's move on to pricing", "Duccio")
    assert b2.get("left") is None
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None
    store.remove(bot_id)


def test_webhook_split_leave_ignores_substantive_address(monkeypatch, tmp_path):
    """Meter safety (code-review repro): a SUBSTANTIVE address ("Cedric hold on a
    second") must NOT arm the split window, so a later same-speaker aside
    dismissing someone else ("Sara you can leave now") can never end the bot."""
    bot_id = "leave-split-4"
    calls = _stub_cedric_webhook(monkeypatch, tmp_path, bot_id)

    _post_line_as(bot_id, "Cedric hold on a second", "Duccio")
    b2 = _post_line_as(bot_id, "Sara you can leave now", "Duccio")
    assert b2.get("left") is None, "a substantive address must not arm the window"
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None
    store.remove(bot_id)


def test_webhook_split_leave_skips_dismissal_of_named_participant(monkeypatch, tmp_path):
    """Meter safety: even after a BARE address, a follow-up that dismisses another
    NAMED participant ("Sara, you can leave") is aimed at Sara, not the avatar —
    it must not end the bot."""
    bot_id = "leave-split-5"
    calls = _stub_cedric_webhook(monkeypatch, tmp_path, bot_id)

    _post_line_as(bot_id, "I think we're just about done", "Sara")  # Sara → roster
    _post_line_as(bot_id, "Cedric.", "Duccio")                      # bare address → armed
    b = _post_line_as(bot_id, "Sara you can leave now", "Duccio")
    assert b.get("left") is None, "dismissing a named participant must not end the bot"
    assert calls["leave"] == 0
    assert store.get(bot_id) is not None
    store.remove(bot_id)
