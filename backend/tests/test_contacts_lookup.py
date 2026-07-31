"""Contacts lookup (PA tier): people_client wire behaviour, ASR spoken-email
normalization, the session gating of the live tool, and the typed-action
grounding seam. Key-free: httpx + the token seam are monkeypatched; nothing
touches the network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import google_client
from app.brain import tools
from app.brain.asr_normalize import normalize_spoken_email
from app.integrations import people_client


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload


def _tok(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda principal, oauth=None, *, on_rotate=None, force_refresh=False:
        (("tok-fresh" if force_refresh else "tok-1"), ""),
    )


def _person(name, *emails):
    return {"person": {
        "names": [{"displayName": name}],
        "emailAddresses": [{"value": e} for e in emails],
    }}


# ── asr normalization ──────────────────────────────────────────────────────

def test_spoken_email_normalized():
    assert normalize_spoken_email(
        "It's duccio at sff studio dot com."
    ) == "It's duccio@sffstudio.com."
    assert normalize_spoken_email(
        "x at sff dot studio dot com"
    ) == "x@sff.studio.com"


def test_prose_never_rewritten():
    for text in (
        "meet me at cafe dot com tomorrow",
        "I was at home dot com no wait",
        "we met at the studio dot",
        "lunch at noon dot",
    ):
        assert normalize_spoken_email(text) == text


def test_real_addresses_pass_through():
    assert normalize_spoken_email("mail a@b.com now") == "mail a@b.com now"
    assert normalize_spoken_email("") == ""


# ── people_client ──────────────────────────────────────────────────────────

def test_search_merges_and_dedupes(monkeypatch):
    _tok(monkeypatch)
    pages = [
        _Resp(200, {"results": [_person("Duccio N", "duccio@sffstudio.com")]}),
        _Resp(200, {"results": [_person("Duccio N", "DUCCIO@sffstudio.com",
                                        "duccio@gmail.com")]}),
    ]
    monkeypatch.setattr(people_client.httpx, "get", lambda *a, **k: pages.pop(0))
    r = people_client.search_contacts("org-1", "duccio")
    assert r["ok"] is True
    emails = [x["email"].lower() for x in r["results"]]
    assert emails == ["duccio@sffstudio.com", "duccio@gmail.com"]  # deduped


def test_missing_scope_yields_reconnect_message(monkeypatch):
    _tok(monkeypatch)
    monkeypatch.setattr(google_client, "_drop_cached_token", lambda k: None)
    monkeypatch.setattr(people_client.httpx, "get", lambda *a, **k: _Resp(403, {}))
    r = people_client.search_contacts("org-1", "duccio")
    assert r["ok"] is False
    assert "reconnect google" in r["error"].lower()


def test_never_raises_and_soft_fails(monkeypatch):
    _tok(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(people_client.httpx, "get", _boom)
    r = people_client.search_contacts("org-1", "duccio")
    assert r["ok"] is False and "RuntimeError" in r["error"]
    assert people_client.search_contacts("org-1", "") == {
        "ok": False, "error": "empty query"
    }
    people_client.warmup("org-1")  # must swallow the same failure silently


# ── the live tool + session gating ─────────────────────────────────────────

class _Session:
    org_id = "org-1"


def test_contacts_lookup_tool_normalizes_and_reports(monkeypatch):
    _tok(monkeypatch)
    seen_queries = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen_queries.append(params.get("query"))
        return _Resp(200, {"results": [_person("Duccio", "duccio@sffstudio.com")]})

    monkeypatch.setattr(people_client.httpx, "get", fake_get)
    out = tools.contacts_lookup("duccio at sff studio dot com", session=_Session())
    assert "duccio@sffstudio.com" in out
    assert all(q == "duccio@sffstudio.com" for q in seen_queries)


def test_contacts_lookup_requires_session_org():
    assert tools.contacts_lookup("duccio", session=None).startswith("error:")


def test_spec_offered_only_when_contacts_live():
    class Live:
        contacts_live = True
        asana_live = False

    class Dead:
        contacts_live = False
        asana_live = False

    names_live = [s["function"]["name"] for s in tools.specs_for(Live())]
    names_dead = [s["function"]["name"] for s in tools.specs_for(Dead())]
    assert "contacts_lookup" in names_live
    assert "contacts_lookup" not in names_dead


# ── the grounding seam: a spoken address becomes typeable ──────────────────

def test_spoken_address_grounds_email_typing():
    from app.brain import engine

    action = {"item": "Send Duccio the meeting summary at "
                      "duccio at sff studio dot com", "owner": "Ananth"}
    source = engine._action_source(action)
    assert "duccio@sffstudio.com" in source
    assert engine._grounded_emails("duccio@sffstudio.com", source) == [
        "duccio@sffstudio.com"
    ]
