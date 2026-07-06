"""Cross-meeting memory ledger: persistence, dedupe, resolution, carryover."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ledger  # noqa: E402

MEET = "https://meet.google.com/abc-defg-hij"


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Point store+ledger at a throwaway sqlite file (mirrors test_store.py)."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    import app.store as store_mod

    importlib.reload(store_mod)
    importlib.reload(ledger)
    yield
    monkeypatch.delenv("LAURA_STORE_PATH", raising=False)
    importlib.reload(store_mod)
    importlib.reload(ledger)


def _artifact(missing, actions=(), decisions=(), meeting_type="customer_onboarding"):
    return {
        "meeting_type": meeting_type,
        "missing_steps": list(missing),
        "actions": list(actions),
        "decisions": list(decisions),
    }


def test_meeting_key_extraction():
    assert ledger.meeting_key(MEET) == "abc-defg-hij"
    assert ledger.meeting_key("https://zoom.us/j/123456789?pwd=x") == "123456789"
    assert ledger.meeting_key("https://example.com/room/7/") == "https://example.com/room/7"


def test_record_and_carryover_and_resolution():
    url = "https://meet.google.com/led-gerte-st1"
    r1 = ledger.record_meeting(
        url,
        "laura",
        "bot-1",
        _artifact(
            missing=["security_approval", "dpa_confirmation"],
            actions=[{"item": "Book the security assessment", "owner": "Daniel", "deadline": "Friday"}],
            decisions=["Go-live set for August 4"],
        ),
    )
    assert r1["added"] == 4 and r1["resolved"] == 0

    brief = ledger.carryover_brief(url)
    assert "security approval" in brief
    assert "DPA confirmation" in brief
    assert "Book the security assessment" in brief
    assert "(owner: Daniel)" in brief and "(due Friday)" in brief
    assert "Go-live set for August 4" in brief

    # Meeting 2 on the same link: security approval covered, DPA still missing.
    r2 = ledger.record_meeting(
        url, "laura", "bot-2", _artifact(missing=["dpa_confirmation"])
    )
    assert r2["resolved"] == 1  # security_approval closed by meeting 2
    assert r2["added"] == 0     # dpa_confirmation already tracked (deduped)

    key = ledger.meeting_key(url)
    open_steps = [i for i in ledger.items(key, status="open") if i["kind"] == "missing_step"]
    assert [i["item"] for i in open_steps] == ["dpa_confirmation"]
    done = [i for i in ledger.items(key, status="done")]
    assert done and done[0]["item"] == "security_approval"
    assert done[0]["resolved_by_bot_id"] == "bot-2"

    brief2 = ledger.carryover_brief(url)
    assert "security approval" not in brief2.split("Decisions")[0]
    assert "DPA confirmation" in brief2


def test_no_history_gives_empty_brief():
    assert ledger.carryover_brief("https://meet.google.com/nev-ermet-bfr") == ""


def test_free_text_actions_never_auto_resolve():
    url = "https://meet.google.com/act-ionst-ay1"
    ledger.record_meeting(
        url, "laura", "bot-1",
        _artifact(missing=[], actions=[{"item": "Send the pricing deck", "owner": "", "deadline": ""}]),
    )
    ledger.record_meeting(url, "laura", "bot-2", _artifact(missing=[]))
    key = ledger.meeting_key(url)
    open_actions = [i for i in ledger.items(key, status="open") if i["kind"] == "action"]
    assert len(open_actions) == 1  # still open until explicitly resolved


def test_resolve_item_api():
    url = "https://meet.google.com/res-olvea-pi1"
    ledger.record_meeting(
        url, "laura", "bot-1", _artifact(missing=[], actions=[{"item": "Ship it", "owner": "", "deadline": ""}])
    )
    key = ledger.meeting_key(url)
    item = ledger.items(key, status="open")[0]
    assert ledger.resolve_item(item["id"], "manual") is True
    assert ledger.resolve_item(item["id"], "manual") is False  # already done
    assert ledger.items(key, status="open") == []


def test_different_meeting_type_does_not_resolve_steps():
    url = "https://meet.google.com/typ-emism-tc1"
    ledger.record_meeting(url, "laura", "bot-1", _artifact(missing=["security_approval"]))
    # Second meeting on the same link but a different (unknown) type — the
    # step must stay open; resolution is only deterministic within one template.
    ledger.record_meeting(url, "laura", "bot-2", _artifact(missing=[], meeting_type=""))
    key = ledger.meeting_key(url)
    assert [i["item"] for i in ledger.items(key, status="open")] == ["security_approval"]
