"""Per-avatar switches must belong to ONE org, never to the deployment.

Audit finding 2026-07-23 (gap #1): ``avatar_capabilities`` and
``avatar_brain_mode`` were keyed by ``avatar_id`` alone. Any logged-in user of
any tenant could flip Laura's Google capability — or her brain mode — and the
change applied to EVERY org on the deployment. The dashboard route did not even
read ``user["org_id"]``.

Both tables are now keyed with ``org_id``. A pre-existing row keeps
``org_id = ''`` and is still honoured as the deployment-wide default, so a live
database behaves exactly as before until an org sets its own value — that
back-compat is asserted here too, because silently changing a running tenant's
capability would be its own incident.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth
from app.persistence import store as store_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store_mod)
    return store_mod


ORG_A = "org-alpha"
ORG_B = "org-beta"


# ── capabilities ───────────────────────────────────────────────────────────

def test_capability_off_in_one_org_leaves_the_other_untouched(store):
    assert store.set_avatar_capability("laura", "google", False, org_id=ORG_A)
    assert store.get_avatar_capabilities("laura", ORG_A) == {"google": False}
    assert store.get_avatar_capabilities("laura", ORG_B) == {}, (
        "org B must not inherit org A's switch"
    )
    assert store.capability_enabled(
        "laura", "google", connected=True, org_id=ORG_B
    ) is True
    assert store.capability_enabled(
        "laura", "google", connected=True, org_id=ORG_A
    ) is False


def test_each_org_keeps_its_own_value_for_the_same_avatar(store):
    store.set_avatar_capability("laura", "slack", True, org_id=ORG_A)
    store.set_avatar_capability("laura", "slack", False, org_id=ORG_B)
    assert store.get_avatar_capabilities("laura", ORG_A) == {"slack": True}
    assert store.get_avatar_capabilities("laura", ORG_B) == {"slack": False}


def test_legacy_row_is_the_default_until_the_org_sets_its_own(store):
    """Back-compat: rows written before this migration have org_id ''."""
    store.set_avatar_capability("laura", "google", False)  # legacy write
    # every org sees the deployment default…
    assert store.get_avatar_capabilities("laura", ORG_A) == {"google": False}
    assert store.get_avatar_capabilities("laura", ORG_B) == {"google": False}
    # …until one of them decides for itself, which must not move the other
    store.set_avatar_capability("laura", "google", True, org_id=ORG_A)
    assert store.get_avatar_capabilities("laura", ORG_A) == {"google": True}
    assert store.get_avatar_capabilities("laura", ORG_B) == {"google": False}


def test_org_row_overrides_only_the_keys_it_sets(store):
    store.set_avatar_capability("laura", "google", False)   # legacy
    store.set_avatar_capability("laura", "slack", False)    # legacy
    store.set_avatar_capability("laura", "google", True, org_id=ORG_A)
    assert store.get_avatar_capabilities("laura", ORG_A) == {
        "google": True,   # overridden
        "slack": False,   # inherited from the deployment default
    }


# ── brain mode ─────────────────────────────────────────────────────────────

def test_brain_mode_is_per_org(store):
    assert store.set_avatar_brain_mode("laura", "cerebras", org_id=ORG_A)
    assert store.get_avatar_brain_mode("laura", ORG_A) == "cerebras"
    assert store.get_avatar_brain_mode("laura", ORG_B) is None


def test_brain_mode_legacy_row_still_read(store):
    store.set_avatar_brain_mode("laura", "cerebras")  # legacy, no org
    assert store.get_avatar_brain_mode("laura", ORG_A) == "cerebras"


# ── the door that let it happen ────────────────────────────────────────────

def test_dashboard_toggle_writes_only_the_callers_org(tmp_path, monkeypatch):
    """The live hole: the route never read user['org_id']."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store_mod)
    import app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    user_a = store_mod.upsert_user("a@alpha.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user_a["user_id"]))

    r = client.post(
        "/dashboard/avatar/laura/capability",
        json={"capability": "google", "enabled": False},
    )
    assert r.status_code == 200, r.text

    org_a = str(user_a["org_id"])
    assert store_mod.get_avatar_capabilities("laura", org_a) == {"google": False}
    # a different tenant is untouched — the whole point of the fix
    assert store_mod.get_avatar_capabilities("laura", "someone-else") == {}
