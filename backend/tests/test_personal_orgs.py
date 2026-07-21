"""Personal-first tenancy: every user gets their OWN workspace.

Owner decision (2026-07-13, re-confirmed 2026-07-16): domain colleagues are
never auto-grouped into one shared org. These tests pin the whole policy:

- two same-domain users resolve to TWO different orgs (both resolver layers),
- the parked shared-domain behavior stays reachable behind
  LAURA_SHARED_DOMAIN_ORGS (flag on = old routing, unchanged),
- each personal org sees only its own meetings/artifacts,
- a fresh login lands on the free entitlement (15 minutes),
- the live-meeting hot path (per-request current_user, attribution lookups)
  never makes a synchronous control-plane (Postgres) call,
- migration 0008 carries the durable half of the same policy.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import auth, avatars, billing, control_plane, entitlements, store
from app.config import settings

DOMAIN_ORG = "org_sff"


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


def _seed_verified_domain(domain: str = "sffstudio.com") -> None:
    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, slug, created_at) "
            "VALUES (?, 'Swiss Founders Fund', 'sff', 1.0)",
            (DOMAIN_ORG,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO org_domains (org_id, domain, verified_at) "
            "VALUES (?, ?, 1.0)",
            (DOMAIN_ORG, domain),
        )


# ── the policy, SQLite resolver (key-free parity) ───────────────────────────

def test_same_domain_users_get_distinct_personal_orgs(monkeypatch, tmp_path):
    """THE acceptance test: two sffstudio.com colleagues → two orgs."""
    _fresh_store(monkeypatch, tmp_path)
    _seed_verified_domain()
    monkeypatch.setattr(settings, "shared_domain_orgs", False)

    a = store.upsert_user("ananth@sffstudio.com")
    b = store.upsert_user("duccio@sffstudio.com")
    assert a["org_id"] != b["org_id"]
    assert a["org_id"] != DOMAIN_ORG and b["org_id"] != DOMAIN_ORG
    # personal invariant: org == the user's own id
    assert a["org_id"] == a["user_id"] and b["org_id"] == b["user_id"]


def test_flag_on_restores_shared_domain_routing(monkeypatch, tmp_path):
    """The parked behavior is behind the flag, byte-identical."""
    _fresh_store(monkeypatch, tmp_path)
    _seed_verified_domain()
    monkeypatch.setattr(settings, "shared_domain_orgs", True)

    a = store.upsert_user("ananth@sffstudio.com")
    b = store.upsert_user("duccio@sffstudio.com")
    assert a["org_id"] == b["org_id"] == DOMAIN_ORG


def test_free_mail_unaffected_by_flag(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    _seed_verified_domain()
    for flag in (False, True):
        monkeypatch.setattr(settings, "shared_domain_orgs", flag)
        u = store.upsert_user("someone@gmail.com")
        assert u["org_id"] == u["user_id"]


# ── the policy, durable resolver (cached uuid, no per-request PG) ───────────

def test_durable_login_caches_personal_uuid_on_user_row(monkeypatch, tmp_path):
    """With the control plane on, ensure_user's PERSONAL uuid is stored on the
    SQLite row at login — afterwards the hot path reads SQLite only."""
    _fresh_store(monkeypatch, tmp_path)
    _seed_verified_domain()
    monkeypatch.setattr(settings, "shared_domain_orgs", False)
    monkeypatch.setattr(control_plane, "enabled", lambda: True)

    orgs = {
        "ananth@sffstudio.com": "11111111-1111-4111-8111-111111111111",
        "duccio@sffstudio.com": "22222222-2222-4222-8222-222222222222",
    }
    monkeypatch.setattr(
        control_plane, "ensure_user",
        lambda sub, email, name="", picture="": {
            "user_id": "33333333-3333-4333-8333-333333333333",
            "org_id": orgs[email], "email": email, "created": True,
        },
    )
    a = store.upsert_user("ananth@sffstudio.com")
    b = store.upsert_user("duccio@sffstudio.com")
    assert a["org_id"] == orgs["ananth@sffstudio.com"]
    assert b["org_id"] == orgs["duccio@sffstudio.com"]
    assert a["org_id"] != b["org_id"]

    # cached: get_user must serve the uuid with Postgres unreachable
    monkeypatch.setattr(control_plane, "_get_engine", _explode)
    row = store.get_user(a["user_id"])
    assert row and row["org_id"] == orgs["ananth@sffstudio.com"]


# ── dashboard isolation ─────────────────────────────────────────────────────

def test_personal_orgs_see_only_their_own_meetings(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    org_a = "11111111-1111-4111-8111-111111111111"
    org_b = "22222222-2222-4222-8222-222222222222"
    store.save_artifact("bot-a", {"org_id": org_a, "summary": "a"}, org_id=org_a)
    store.save_artifact("bot-b", {"org_id": org_b, "summary": "b"}, org_id=org_b)

    a_rows = {r["bot_id"] for r in store.list_artifacts(org_id=org_a)}
    b_rows = {r["bot_id"] for r in store.list_artifacts(org_id=org_b)}
    assert a_rows == {"bot-a"} and b_rows == {"bot-b"}
    assert store.get_artifact("bot-b", org_id=org_a) is None  # cross-read blocked


# ── the free entitlement ────────────────────────────────────────────────────

def test_fresh_personal_org_gets_free_minutes(monkeypatch):
    """Key-free parity: a brand-new personal org sees the 15-minute free tier
    and can start a session. (The durable side seeds billing_accounts 900s in
    ensure_user's personal bundle — asserted on the migration below.)"""
    data = billing._summary_for_org("44444444-4444-4444-8444-444444444444", None)
    assert data["plan"] == "free"
    assert data["included_seconds"] == settings.free_trial_seconds == 900
    assert data["can_start_session"] is True


def test_personal_org_sees_all_avatars(monkeypatch, tmp_path):
    """No org_agents grants for a fresh personal org → fail-open to every
    installed avatar (Laura included) — the 'gets Laura' guarantee."""
    _fresh_store(monkeypatch, tmp_path)
    ids = avatars.list_for_org("55555555-5555-4555-8555-555555555555")
    assert "laura" in ids


# ── hot path: never a synchronous control-plane call ────────────────────────

def _explode(*a, **k):
    raise AssertionError("hot path must never touch the control plane")


def test_per_request_current_user_reads_sqlite_only(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "test-secret")
    u = store.upsert_user("hotpath@gmail.com")

    monkeypatch.setattr(control_plane, "_get_engine", _explode)
    monkeypatch.setattr(entitlements, "_engine", _explode)

    request = SimpleNamespace(
        cookies={auth.COOKIE_NAME: auth.make_cookie(u["user_id"])}
    )
    got = auth.current_user(request)  # the per-request/live-path lookup
    assert got and got["org_id"] == u["org_id"]


def test_attribution_lookup_is_sqlite_only(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    store.upsert_user("owner@gmail.com")
    monkeypatch.setattr(control_plane, "_get_engine", _explode)
    monkeypatch.setattr(entitlements, "_engine", _explode)
    org = store.org_for_email("owner@gmail.com")
    assert org  # resolved without any Postgres round-trip


# ── attribution: the person wins over legacy org connections ────────────────

def test_org_for_email_prefers_registered_user(monkeypatch, tmp_path):
    """A person's own org beats a legacy org-level Google connection that
    carries the same address — the meter lands on the owner."""
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "sek")

    u = store.upsert_user("person@sffstudio.com")
    legacy_org = "99999999-9999-4999-8999-999999999999"
    assert store.set_org_oauth(legacy_org, "rt", email="person@sffstudio.com")

    assert store.org_for_email("person@sffstudio.com") == u["org_id"]


def test_org_oauth_still_resolves_never_logged_in_addresses(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "sek")
    org = "88888888-8888-4888-8888-888888888888"
    assert store.set_org_oauth(org, "rt", email="shared-inbox@acme.com")
    assert store.org_for_email("shared-inbox@acme.com") == org


# ── security: the gmail-invite path never attributes from a mail header ─────

def test_gmail_watcher_does_not_expose_sender_for_attribution():
    """B1 guard (adversarial review 2026-07-16): Reply-To/From are
    unauthenticated and the watcher reads one shared inbox, so the sender must
    never become an org-attribution signal. poll_new_invites returns exactly
    the 4-tuple (no sender element) and the module exposes no sender helper -
    a future dev re-adding one has to defeat this test on purpose."""
    from app import gmail_watcher

    assert not hasattr(gmail_watcher, "_sender_addresses")
    assert not hasattr(gmail_watcher, "_SENDER_HEADERS")

    listing = {"messages": [{"id": "m1"}]}
    message = {
        "snippet": "join https://meet.google.com/abc-defg-hij",
        "internalDate": "1783674000000",
        "payload": {
            "headers": [
                {"name": "To", "value": "laura.ai.122222@gmail.com"},
                # a FORGED Reply-To naming a victim; must be inert
                {"name": "Reply-To", "value": "victim@sffstudio.com"},
                {"name": "From", "value": "attacker@evil.test"},
            ],
            "parts": [],
        },
    }

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    import pytest as _pytest

    def fake_get(url, **kwargs):
        return _Resp(message if "/messages/" in url else listing)

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            gmail_watcher, "_client",
            type("C", (), {"get": staticmethod(fake_get)}),
        )
        out = gmail_watcher.poll_new_invites("tok", set())
    assert len(out) == 1
    assert len(out[0]) == 4  # (mid, url, recipients, received_at); no sender
    # the forged victim address appears nowhere in the emitted tuple
    assert "victim@sffstudio.com" not in str(out[0])


# ── migration 0008: the durable half carries the same policy ────────────────

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / (
    "0008_personal_orgs_policy.py"
)


def test_migration_gates_domain_branch_and_keeps_personal_bundle():
    text = MIGRATION.read_text()
    # the gate: domain lookup only under the policy flag
    assert "COALESCE(v_shared_domain_orgs, false) AND v_domain" in text
    # policy storage + exact-entry-point setter, runtime-role only
    assert "laura_private.policy_settings" in text
    assert "set_shared_domain_orgs" in text
    assert "TO laura_app" in text
    assert "FROM PUBLIC" in text
    # the personal bundle still seeds the free entitlement + Laura
    assert "'free', 900" in text
    assert "'laura'" in text
    # seeded default is personal-first (safe direction on failed boot-sync)
    assert "shared_domain_orgs boolean NOT NULL DEFAULT false" in text


def test_sync_policy_flags_pushes_env_value(monkeypatch):
    calls = {}

    class _Conn:
        def execute(self, sql, params):
            calls["sql"] = str(sql)
            calls["params"] = params

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    class _Engine:
        def begin(self):
            return _Ctx()

    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "_get_engine", lambda: _Engine())
    monkeypatch.setattr(settings, "shared_domain_orgs", False)
    assert control_plane.sync_policy_flags() is True
    assert "set_shared_domain_orgs" in calls["sql"]
    assert calls["params"] == {"v": False}


def test_sync_policy_flags_failure_never_raises(monkeypatch):
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(
        control_plane, "_get_engine",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert control_plane.sync_policy_flags() is False  # logged, not raised
