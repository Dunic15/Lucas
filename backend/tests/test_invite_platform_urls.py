"""Gmail watcher extracts Zoom/Teams invite links, not just Meet — key-free.

A forwarded Zoom or Teams invitation email must auto-join exactly like Meet's
"Add people" does. Join credentials embedded in the link (?pwd= passcode,
meetup-join context) must survive extraction — a stripped Zoom link strands
the bot at the passcode screen.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import gmail_watcher  # noqa: E402


# ── _extract_meeting_urls: one platform at a time ──
def test_extract_meet_urls_rebuilt_from_code():
    text = "Join here: https://meet.google.com/abc-defg-hij?hs=122&authuser=0"
    assert gmail_watcher._extract_meeting_urls(text) == {
        "https://meet.google.com/abc-defg-hij"
    }


def test_extract_zoom_urls_keep_pwd():
    text = 'Zoom: <a href="https://us02web.zoom.us/j/85012345678?pwd=aBcDe.123">join</a>'
    assert gmail_watcher._extract_meeting_urls(text) == {
        "https://us02web.zoom.us/j/85012345678?pwd=aBcDe.123"
    }


def test_extract_teams_meetup_join_keep_context():
    url = (
        "https://teams.microsoft.com/l/meetup-join/"
        "19%3ameeting_NzJjZjE0%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d"
    )
    assert gmail_watcher._extract_meeting_urls(f"Click to join: {url}.") == {url}


def test_extract_teams_live_meet_urls_trailing_punctuation():
    text = "Meeting link: https://teams.live.com/meet/9312345678901?p=AbCdEf,"
    assert gmail_watcher._extract_meeting_urls(text) == {
        "https://teams.live.com/meet/9312345678901?p=AbCdEf"
    }


def test_extract_mixed_platforms_in_one_email():
    text = (
        "Meet https://meet.google.com/abc-defg-hij "
        "or Zoom https://zoom.us/j/85012345678 "
        "or Teams https://teams.live.com/meet/9312345678901"
    )
    assert gmail_watcher._extract_meeting_urls(text) == {
        "https://meet.google.com/abc-defg-hij",
        "https://zoom.us/j/85012345678",
        "https://teams.live.com/meet/9312345678901",
    }


# ── poll_new_invites end-to-end on a Zoom invite email ──
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_poll_new_invites_zoom_email(monkeypatch):
    listing = {"messages": [{"id": "z1"}]}
    message = {
        "snippet": "Duccio is inviting you to a Zoom meeting.",
        "internalDate": "1783674000000",
        "payload": {
            "headers": [
                {"name": "To", "value": "Laura <laura.ai.122222@gmail.com>"},
            ],
            "parts": [
                {
                    "body": {
                        # base64url of: Join: https://us05web.zoom.us/j/86395749732?pwd=Jg.rSx15
                        "data": "Sm9pbjogaHR0cHM6Ly91czA1d2ViLnpvb20udXMvai84NjM5NTc0OTczMj9wd2Q9SmcuclN4MTU",
                    },
                    "parts": [],
                }
            ],
        },
    }

    def fake_get(url, **kwargs):
        return _FakeResp(message if "/messages/" in url else listing)

    monkeypatch.setattr(
        gmail_watcher, "_client", type("C", (), {"get": staticmethod(fake_get)})
    )
    out = gmail_watcher.poll_new_invites("tok", set())
    assert len(out) == 1
    mid, url, addrs, received_at = out[0]
    assert mid == "z1"
    assert url == "https://us05web.zoom.us/j/86395749732?pwd=Jg.rSx15"
    assert received_at == 1783674000.0
    assert "laura.ai.122222@gmail.com" in addrs


def test_invite_query_covers_all_platforms():
    q = gmail_watcher._INVITE_QUERY
    for host in ("meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com"):
        assert host in q
    assert q.startswith("newer_than:1h ")
