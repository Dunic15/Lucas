"""Two-org isolation on SQLite — the safety thesis of the org_id spine.

Seeds org A and org B on the SAME recurring meeting link (the cross-org
meeting_key merge that would otherwise leak another tenant's open items into the
LIVE prompt, MULTI-TENANCY §6.4) and proves B can read NONE of A's rows through
every store/ledger enumeration, and can resolve none of A's items. Also proves
the demo/service path (demo_org_id) still works unchanged — single-tenant is
byte-identical.

Key-free like the rest of the suite: sqlite in tmp_path, no vendors touched.
On SQLite RLS is a no-op, so this exercises the belt (`WHERE org_id=?`); the
suspenders (Postgres RLS FORCE) are the future PR6 Testcontainers job.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ORG_A = "org-aaaaaaaa-1111"
ORG_B = "org-bbbbbbbb-2222"
# Same link for both tenants — this is exactly the merge the org scope prevents.
SHARED_URL = "https://meet.google.com/iso-late-tst"


@pytest.fixture
def mt(tmp_path, monkeypatch):
    """Fresh store+ledger on a throwaway sqlite file (mirrors test_ledger.py)."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    import app.store as store

    store = importlib.reload(store)
    import app.ledger as ledger

    ledger = importlib.reload(ledger)
    yield store, ledger
    monkeypatch.delenv("LAURA_STORE_PATH", raising=False)
    importlib.reload(store)
    importlib.reload(ledger)


def _artifact(action_item, action_id, decision, summary):
    return {
        "summary": summary,
        "actions": [{"item": action_item, "owner": "Owner", "action_id": action_id}],
        "decisions": [decision],
        "missing_steps": [],
        "meeting_type": "status_update",
    }


def _seed(store, ledger, org, bot, action_item, action_id, decision, summary):
    art = _artifact(action_item, action_id, decision, summary)
    art["org_id"] = org
    art["meeting_url"] = SHARED_URL
    store.save_artifact(bot, art, org_id=org)
    ledger.record_meeting(SHARED_URL, "laura", bot, art, org_id=org)


@pytest.fixture
def seeded(mt):
    store, ledger = mt
    _seed(store, ledger, ORG_A, "bot_a", "Ship the A rollout", "aid_a1",
          "Org A pricing decided", "Org A weekly sync")
    _seed(store, ledger, ORG_B, "bot_b", "Ship the B rollout", "aid_b1",
          "Org B launch decided", "Org B weekly sync")
    return store, ledger


# ── artifacts ──

def test_list_artifacts_is_org_scoped(seeded):
    store, _ = seeded
    a_bots = {r["bot_id"] for r in store.list_artifacts(ORG_A)}
    b_bots = {r["bot_id"] for r in store.list_artifacts(ORG_B)}
    assert a_bots == {"bot_a"}
    assert b_bots == {"bot_b"}
    assert "bot_a" not in b_bots  # B never sees A's meeting


# ── ledger: carryover_brief feeds the LIVE prompt (highest blast radius) ──

def test_carryover_brief_never_crosses_orgs(seeded):
    _, ledger = seeded
    brief_a = ledger.carryover_brief(SHARED_URL, org_id=ORG_A)
    brief_b = ledger.carryover_brief(SHARED_URL, org_id=ORG_B)
    assert "A rollout" in brief_a and "A pricing" in brief_a
    assert "B rollout" not in brief_a  # A's live prompt is clean of B
    assert "B rollout" in brief_b and "B launch" in brief_b
    assert "A rollout" not in brief_b  # B's live prompt is clean of A


def test_items_and_open_by_meeting_scoped(seeded):
    _, ledger = seeded
    key = ledger.meeting_key(SHARED_URL)
    a_items = [i["item"] for i in ledger.items(key, org_id=ORG_A)]
    b_items = [i["item"] for i in ledger.items(key, org_id=ORG_B)]
    assert any("A rollout" in i for i in a_items)
    assert not any("B rollout" in i for i in a_items)
    assert any("B rollout" in i for i in b_items)
    assert not any("A rollout" in i for i in b_items)

    open_b = ledger.open_by_meeting(org_id=ORG_B)
    flat = [i["item"] for items in open_b.values() for i in items]
    assert any("B rollout" in i for i in flat)
    assert not any("A rollout" in i for i in flat)


def test_search_scoped(seeded):
    _, ledger = seeded
    hits_b = ledger.search("rollout", org_id=ORG_B)
    assert hits_b and all("A rollout" not in h["item"] for h in hits_b)
    assert any("B rollout" in h["item"] for h in hits_b)


# ── ledger: B cannot RESOLVE A's items (write isolation) ──

def test_org_b_cannot_resolve_org_a_item(seeded):
    _, ledger = seeded
    key = ledger.meeting_key(SHARED_URL)
    a_item = next(i for i in ledger.items(key, org_id=ORG_A) if i["kind"] == "action")

    # B tries to resolve A's numeric row id → refused, A's item stays open.
    assert ledger.resolve_item(a_item["id"], "bob", "done", org_id=ORG_B) is False
    # B tries via A's stable action_id → also refused.
    assert ledger.resolve_by_action_id("aid_a1", "bob", "done", org_id=ORG_B) is False
    still_open = [i["id"] for i in ledger.items(key, status="open", org_id=ORG_A)]
    assert a_item["id"] in still_open  # untouched

    # A can resolve its own item (control: the WHERE isn't just always-false).
    assert ledger.resolve_item(a_item["id"], "alice", "done", org_id=ORG_A) is True
    assert a_item["id"] not in [
        i["id"] for i in ledger.items(key, status="open", org_id=ORG_A)
    ]


def test_shared_meeting_key_does_not_merge_or_evict(seeded):
    """Same meeting_key + kind + normalized text under two orgs must COEXIST
    (the dedupe UNIQUE and the eviction DELETE are both org-scoped, §6.5)."""
    store, ledger = seeded
    # Re-record identical text for BOTH orgs — dedupe is per-org, so each keeps
    # its own single copy; neither deletes the other's row.
    same = _artifact("Book the venue", "aid_same", "Same decision", "sync")
    ledger.record_meeting(SHARED_URL, "laura", "bot_a2", {**same, "org_id": ORG_A},
                          org_id=ORG_A)
    ledger.record_meeting(SHARED_URL, "laura", "bot_b2", {**same, "org_id": ORG_B},
                          org_id=ORG_B)
    key = ledger.meeting_key(SHARED_URL)
    a_venue = [i for i in ledger.items(key, org_id=ORG_A) if i["item"] == "Book the venue"]
    b_venue = [i for i in ledger.items(key, org_id=ORG_B) if i["item"] == "Book the venue"]
    assert len(a_venue) == 1 and len(b_venue) == 1  # both present, neither evicted


# ── demo / single-tenant path unchanged ──

def test_demo_org_path_still_works(mt):
    store, ledger = mt
    demo = store.DEMO_ORG_ID
    url = "https://meet.google.com/demo-single-ten"
    art = _artifact("Demo action", "aid_demo", "Demo decision", "demo sync")
    # Default-arg callers (no org passed) == the service/demo path.
    store.save_artifact("bot_demo", art)          # column derives → demo
    ledger.record_meeting(url, "laura", "bot_demo", art)  # org defaults → demo

    assert ledger.carryover_brief(url) != ""      # demo default reads its own rows
    assert "Demo action" in ledger.carryover_brief(url)
    key = ledger.meeting_key(url)
    assert any(i["item"] == "Demo action" for i in ledger.items(key))
    assert {r["bot_id"] for r in store.list_artifacts(demo)} >= {"bot_demo"}
    # explicit demo == the default, and both isolate from A/B
    assert ledger.carryover_brief(url, org_id=ORG_A) == ""


def test_demo_org_row_seeded(mt):
    store, _ = mt
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            "SELECT name, slug FROM orgs WHERE id=?", (store.DEMO_ORG_ID,)
        ).fetchone()
    assert row is not None and row["slug"] == "demo"
