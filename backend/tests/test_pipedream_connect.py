"""Pipedream Connect client — config gating, token caching, catalog search,
connect-link minting, and the accounts→mcporter sync that turns dashboard
connections into OpenClaw agent toolsets. Key-free: httpx is mocked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.integrations import pipedream_client as pd  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_test")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "csecret")
    monkeypatch.setattr(settings, "pipedream_external_user_id", "laura-dev")
    monkeypatch.setattr(
        settings, "mcporter_config_path", str(tmp_path / "mcporter.json")
    )
    pd.reset_token_cache_for_tests()


def test_disabled_without_creds(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_project_id", "")
    assert pd.enabled() is False
    for result in (
        pd.search_apps("slack"),
        pd.list_accounts(),
        pd.create_connect_link("slack"),
    ):
        assert result["ok"] is False and "not configured" in result["error"]


def test_search_and_token_cache(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    posts: list[str] = []

    def fake_post(url, **kw):
        posts.append(url)
        return _Resp({"access_token": "tok-1"})

    def fake_get(url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer tok-1"
        return _Resp({"data": [
            {"name_slug": "slack", "name": "Slack", "auth_type": "oauth"},
            {"no_slug": True},
        ]})

    monkeypatch.setattr(pd.httpx, "post", fake_post)
    monkeypatch.setattr(pd.httpx, "get", fake_get)

    r = pd.search_apps("sla")
    assert r["ok"] is True and r["apps"] == [
        {"slug": "slack", "name": "Slack", "description": "", "auth_type": "oauth"}
    ]
    pd.search_apps("sla")  # second call reuses the cached token
    assert len(posts) == 1


def test_connect_link(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pd.httpx, "post",
        lambda url, **kw: _Resp(
            {"access_token": "t"} if url.endswith("/oauth/token")
            else {"connect_link_url": "https://pd/connect?token=ctok_1", "expires_at": "soon"}
        ),
    )
    r = pd.create_connect_link("hubspot")
    assert r["ok"] is True and r["url"].endswith("&app=hubspot")
    assert pd.create_connect_link("")["ok"] is False


def test_sync_mcporter(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "openclaw_executor", True)
    cfg = tmp_path / "mcporter.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "pd-stale": {"command": "bash", "args": ["x"]},
            "custom": {"baseUrl": "https://keep.me"},
        }
    }))
    monkeypatch.setattr(pd.httpx, "post", lambda url, **kw: _Resp({"access_token": "t"}))
    monkeypatch.setattr(
        pd.httpx, "get",
        lambda url, **kw: _Resp({"data": [
            {"app": {"name_slug": "asana"}, "name": "a@b.c", "healthy": True},
            {"app": {"name_slug": "asana"}, "name": "dup@b.c", "healthy": True},
            {"app": {"name_slug": "notion"}, "name": "n@b.c", "healthy": False},
        ]}),
    )

    r = pd.sync_mcporter()
    assert r["ok"] is True
    written = json.loads(cfg.read_text())["mcpServers"]
    # Healthy app synced (deduped), unhealthy skipped, stale pd-* dropped,
    # non-pd entry preserved.
    assert set(written) == {"pd-asana", "custom"}
    assert written["pd-asana"]["args"][1:] == ["laura-dev", "asana"]
    assert written["custom"] == {"baseUrl": "https://keep.me"}


def test_sync_gated_on_openclaw(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "openclaw_executor", False)
    r = pd.sync_mcporter()
    assert r["ok"] is False and "openclaw_executor" in r["error"]
