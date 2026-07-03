"""Duplicate Recall bot reconciliation tests. No API calls, no secrets needed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.config import settings  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _bot(bot_id: str, *, created_at: str, variant: dict | None) -> dict:
    return {
        "id": bot_id,
        "created_at": created_at,
        "variant": variant,
        "meeting_url": {"meeting_id": "abc-defg-hij"},
        "status_changes": [{"code": "in_call_recording"}],
    }


def test_bot_variant_rank_prefers_high_performance_variants():
    assert main._bot_variant_rank({"variant": {"google_meet": "web_gpu"}}) == 0
    assert main._bot_variant_rank({"variant": {"google_meet": "web_4_core"}}) == 1
    assert main._bot_variant_rank({"variant": {"google_meet": "future_paid"}}) == 2
    assert main._bot_variant_rank({"variant": None}) == 3


def test_reconcile_keeps_web_4_core_over_older_default(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_base", "https://eu-central-1.recall.ai")
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")

    captured = {}

    def fake_get(url, *, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "results": [
                    _bot("old-default", created_at="2026-07-03T09:00:00Z", variant=None),
                    _bot(
                        "new-4-core",
                        created_at="2026-07-03T09:01:00Z",
                        variant={"google_meet": "web_4_core"},
                    ),
                ]
            }
        )

    left = []
    monkeypatch.setattr(main.httpx, "get", fake_get)
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)

    main._reconcile_duplicate_bots("https://meet.google.com/abc-defg-hij", "new-4-core")

    assert captured["url"] == "https://eu-central-1.recall.ai/api/v1/bot/"
    assert captured["headers"]["Authorization"] == "recall-key"
    assert left == ["old-default"]
