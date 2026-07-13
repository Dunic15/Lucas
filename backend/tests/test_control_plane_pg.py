"""Two-org isolation on the REAL control plane — embedded Postgres (PR A).

Boots an embedded Postgres (pgserver), runs the full Alembic chain through
0004 with an ADMIN URL, then points the runtime DAL at a distinct
``laura_app`` URL and proves on real RLS:

- two self-serve signups (control_plane.ensure_user) produce two DISTINCT UUID
  personal orgs, each with an owner membership, exactly the laura/cedric
  org_agents grants, and a free billing account (900 included seconds);
- signup is idempotent (re-login by sub OR by email returns the same ids);
- per-org machine tokens round-trip and NEVER cross orgs;
- runtime ``current_user`` is exactly ``laura_app``, with both
  ``rolsuper`` and ``rolbypassrls`` false;
- signup/domain and token resolution work only through the private definer
  functions while direct identity enumeration is denied;
- FORCE RLS isolates reads and rejects cross-org writes.

Skipped when pgserver isn't installed (CI installs it; the key-free suite is
otherwise untouched). Embedded Postgres ships without contrib extensions, so
the fixture writes tiny SHIM control files for pgcrypto (gen_random_uuid() is
core since PG13 — the shim creates nothing) and citext (a text domain — case
folding is done app-side; nothing here relies on citext collation). Supabase
has the real extensions; the shims exist only so 0001 runs VERBATIM here.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.config import settings  # noqa: E402

pytestmark = pytest.mark.pg

BACKEND_DIR = Path(__file__).resolve().parents[1]

APP_ROLE = "laura_app"
APP_ROLE_PASSWORD = "laura-app-test-pw"


def _write_extension_shims() -> None:
    """See the module docstring: make `CREATE EXTENSION pgcrypto/citext`
    succeed on contrib-less embedded Postgres without touching 0001."""
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall"
        / "share"
        / "postgresql"
        / "extension"
    )
    shims = {
        "pgcrypto.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim: gen_random_uuid() is core since PG13'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim: no objects; gen_random_uuid() is core\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim: citext as a plain-text domain'\n"
        ),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():  # never clobber a real contrib install
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    """Embedded PG migrated as admin, plus the distinct runtime app URL."""
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("cp_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        # 0001 REVOKEs on audit_log FROM laura_app — the role must pre-exist,
        # exactly as it must on Supabase before running migrations.
        conn.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        conn.execute("CREATE ROLE anon NOLOGIN")
        conn.execute("CREATE ROLE authenticated NOLOGIN")
    admin_sa_url = uri.replace("postgresql://", "postgresql+psycopg://")

    import psycopg.conninfo as _ci
    from sqlalchemy.engine import URL

    info = _ci.conninfo_to_dict(uri)
    query = {
        key: str(info[key])
        for key in ("host", "port")
        if info.get(key) is not None
    }
    app_sa_url = URL.create(
        "postgresql+psycopg",
        username=APP_ROLE,
        password=APP_ROLE_PASSWORD,
        database=info.get("dbname"),
        query=query,
    ).render_as_string(hide_password=False)

    migrate_env = {
        **os.environ,
        "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
        "LAURA_DATABASE_URL": "",
        "LAURA_REQUIRE_MIGRATIONS": "1",
    }

    def migrate(revision: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=str(BACKEND_DIR),
            env=migrate_env,
            capture_output=True,
            text=True,
            timeout=600,
        )

    # Stop immediately before the privilege-boundary migration and simulate a
    # dirty database whose runtime/public roles were accidentally over-granted.
    proc = migrate("0003_usage")
    assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            "GRANT ALL ON TABLE users, orgs, memberships, org_domains, "
            "org_agents, org_tokens, org_connections, billing_accounts, "
            "usage_sessions, stripe_events, audit_log "
            "TO PUBLIC, laura_app, anon, authenticated"
        )

    # 0004 must be an exact privilege reset, not merely additive hardening.
    proc = migrate("head")
    assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    yield {
        "uri": uri,
        "admin_sa_url": admin_sa_url,
        "app_sa_url": app_sa_url,
    }
    control_plane.reset_engine()
    srv.cleanup()


@pytest.fixture
def cp(pg, monkeypatch):
    """control_plane pointed at the embedded PG (reset back afterwards so the
    rest of the suite stays key-free/disabled)."""
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    control_plane.reset_engine()
    yield control_plane
    control_plane.reset_engine()


def _signup_a(cp):
    return cp.ensure_user("sub-alice", "alice@freemail.test", "Alice", "")


def _signup_b(cp):
    return cp.ensure_user("sub-bob", "bob@freemail.test", "Bob", "")


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


def _app(pg):
    import psycopg.conninfo as _ci

    kwargs = {
        **_ci.conninfo_to_dict(pg["uri"]),
        "user": APP_ROLE,
        "password": APP_ROLE_PASSWORD,
    }
    return psycopg.connect(**kwargs, autocommit=True)


# ── signup: two users → two orgs, fully provisioned ────────────────────

def test_two_signups_two_distinct_uuid_orgs(cp, pg):
    a, b = _signup_a(cp), _signup_b(cp)
    assert a["created"] is True and b["created"] is True
    # real UUIDs, and no sharing between the two signups
    for ids in (a, b):
        uuid.UUID(ids["user_id"])
        uuid.UUID(ids["org_id"])
    assert a["org_id"] != b["org_id"]
    assert a["user_id"] != b["user_id"]

    with _admin(pg) as conn:
        for ids in (a, b):
            row = conn.execute(
                "SELECT role, status FROM memberships "
                "WHERE user_id = %s AND org_id = %s",
                (ids["user_id"], ids["org_id"]),
            ).fetchone()
            assert row == ("owner", "active")
            grants = {
                r[0]
                for r in conn.execute(
                    "SELECT avatar_id FROM org_agents WHERE org_id = %s",
                    (ids["org_id"],),
                ).fetchall()
            }
            assert grants == {"laura", "cedric"}
            billing = conn.execute(
                "SELECT plan, included_seconds FROM billing_accounts "
                "WHERE org_id = %s",
                (ids["org_id"],),
            ).fetchone()
            assert billing == ("free", 900)
        plan = conn.execute(
            "SELECT plan FROM orgs WHERE id = %s", (a["org_id"],)
        ).fetchone()
        assert plan == ("free",)


def test_signup_is_idempotent(cp):
    first = _signup_a(cp)
    again = _signup_a(cp)
    assert again["created"] is False
    assert again["user_id"] == first["user_id"]
    assert again["org_id"] == first["org_id"]
    # email-only lookup (no sub — pre-OIDC caller) lands on the same identity
    by_email = cp.ensure_user("", "alice@freemail.test", "Alice", "")
    assert by_email["user_id"] == first["user_id"]
    assert by_email["org_id"] == first["org_id"]


def test_concurrent_same_sub_signup_single_identity(cp, pg):
    """Adversarial-review blocker 1 (2026-07-13): concurrent same-sub signups
    used to lose on uq_users_google_sub (the INSERT named only the email
    arbiter) and raise IntegrityError — the losing login silently fell back
    to the legacy u_<hash> org. With the arbiter-less ON CONFLICT DO NOTHING
    every racer must converge on ONE user, ONE org, ONE billing account —
    and none may raise."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    n = 12
    barrier = threading.Barrier(n)

    def signup(_):
        barrier.wait()  # maximize the collision window
        return cp.ensure_user("sub-race", "race@freemail.test", "Race", "")

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(signup, range(n)))

    assert all(r is not None for r in results)
    assert len({r["user_id"] for r in results}) == 1
    assert len({r["org_id"] for r in results}) == 1
    org_id = results[0]["org_id"]
    with _admin(pg) as conn:
        n_users = conn.execute(
            "SELECT count(*) FROM users WHERE email = 'race@freemail.test'"
        ).fetchone()[0]
        n_owned = conn.execute(
            "SELECT count(*) FROM memberships WHERE user_id = %s AND role='owner'",
            (results[0]["user_id"],),
        ).fetchone()[0]
        n_billing = conn.execute(
            "SELECT count(*) FROM billing_accounts WHERE org_id = %s", (org_id,)
        ).fetchone()[0]
    assert (n_users, n_owned, n_billing) == (1, 1, 1)


def test_sub_matched_relogin_refreshes_email(cp, pg):
    """Should-fix B (2026-07-13): the sub is the durable key — a re-login
    carrying a NEW address (Google-account email change) must refresh
    users.email, not keep the signup-era one forever."""
    first = cp.ensure_user("sub-emailmove", "before@freemail.test", "Em", "")
    moved = cp.ensure_user("sub-emailmove", "after@freemail.test", "Em", "")
    assert moved["user_id"] == first["user_id"]
    assert moved["org_id"] == first["org_id"]
    assert moved["email"] == "after@freemail.test"
    with _admin(pg) as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id = %s", (first["user_id"],)
        ).fetchone()[0]
    assert email == "after@freemail.test"


def test_org_plan_reads_billing(cp):
    a = _signup_a(cp)
    assert cp.org_plan(a["org_id"]) == {"plan": "free", "included_seconds": 900}
    assert cp.org_plan(str(uuid.uuid4())) is None  # no billing row → None


def test_verified_domain_resolves_to_shared_org(cp, pg):
    """A login whose VERIFIED domain is in org_domains joins THAT org as a
    member — no personal org is created (mirrors store.org_id_for_email)."""
    shared = str(uuid.uuid4())
    with _admin(pg) as conn:
        conn.execute("SELECT set_config('app.current_org', %s, false)", (shared,))
        conn.execute(
            "INSERT INTO orgs (id, name, slug, plan) VALUES (%s, 'Acme', %s, 'free')",
            (shared, f"acme-{shared[:8]}"),
        )
        conn.execute(
            "INSERT INTO org_domains (org_id, domain, verified_at) "
            "VALUES (%s, 'acme.test', now())",
            (shared,),
        )
    user = cp.ensure_user("sub-corp", "employee@acme.test", "Emp", "")
    assert user["org_id"] == shared
    with _admin(pg) as conn:
        row = conn.execute(
            "SELECT role FROM memberships WHERE user_id = %s AND org_id = %s",
            (user["user_id"], shared),
        ).fetchone()
        assert row == ("member",)


# ── per-org machine tokens ─────────────────────────────────────────────

def test_org_token_roundtrip_and_no_cross_org(cp):
    a, b = _signup_a(cp), _signup_b(cp)
    raw_a = cp.mint_org_token(a["org_id"], "ci-a")
    raw_b = cp.mint_org_token(b["org_id"], "ci-b")
    assert raw_a and raw_b and raw_a != raw_b
    assert cp.resolve_org_token(raw_a) == a["org_id"]
    assert cp.resolve_org_token(raw_b) == b["org_id"]
    # org B's token NEVER resolves to org A (and garbage resolves to nothing)
    assert cp.resolve_org_token(raw_b) != a["org_id"]
    assert cp.resolve_org_token("garbage-token") is None
    assert cp.resolve_org_token("") is None


def test_org_token_rotate_and_revoke_work_as_app_role(cp):
    a, b = _signup_a(cp), _signup_b(cp)
    first_a = cp.mint_org_token(a["org_id"], "cedric:workspace")
    token_b = cp.rotate_org_token(b["org_id"], "cedric:workspace")
    assert cp.resolve_org_token(first_a) == a["org_id"]
    assert cp.resolve_org_token(token_b) == b["org_id"]

    rotated_a = cp.rotate_org_token(a["org_id"], "cedric:workspace")
    assert rotated_a and rotated_a != first_a
    assert cp.resolve_org_token(first_a) is None
    assert cp.resolve_org_token(rotated_a) == a["org_id"]
    assert cp.resolve_org_token(token_b) == b["org_id"]

    assert cp.revoke_org_tokens(a["org_id"], "cedric:workspace") is True
    assert cp.resolve_org_token(rotated_a) is None
    assert cp.resolve_org_token(token_b) == b["org_id"]


def test_brain_install_postgres_cas_retry_and_stale_state(cp):
    a = cp.ensure_user(
        "sub-install-cas", "install-cas@freemail.test", "Install CAS", ""
    )
    org = a["org_id"]
    first_raw = "first-deterministic-token"
    second_raw = "second-deterministic-token"

    assert cp.begin_brain_install(org, "cedric", "nonce-1", "#ops") is True
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-1", first_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "applied"
    )
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-1", first_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "replay"
    )
    assert cp.resolve_org_token(first_raw) == org
    # A same-nonce replay is valid only for the exact accepted workspace and
    # both credential fingerprints; mismatches are rejected before rotation.
    for envelope in (
        ("T_OTHER", "#ops", "secret-a", "peer-a"),
        ("T_A", "#other", "secret-a", "peer-a"),
        ("T_A", "#ops", "secret-other", "peer-a"),
        ("T_A", "#ops", "secret-a", "peer-other"),
    ):
        assert (
            cp.accept_brain_install(
                org, "cedric", "nonce-1", first_raw, *envelope
            )
            == "conflict"
        )

    assert cp.begin_brain_install(org, "cedric", "nonce-2", "#ops") is True
    # The newer pending nonce defeats an otherwise valid retry of nonce-1.
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-1", first_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "stale"
    )
    assert cp.resolve_org_token(first_raw) == org
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-2", second_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "applied"
    )
    assert cp.resolve_org_token(first_raw) is None
    assert cp.resolve_org_token(second_raw) == org
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-1", first_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "stale"
    )
    assert cp.resolve_org_token(second_raw) == org
    assert cp.finish_brain_install(org, "cedric", "nonce-2") == "connected"
    assert cp.begin_brain_disconnect(org, "cedric", "remote_revoked") is True
    assert cp.tombstone_brain_install(org, "cedric") is True
    assert (
        cp.accept_brain_install(
            org, "cedric", "nonce-2", second_raw, "T_A", "#ops",
            "secret-a", "peer-a",
        )
        == "stale"
    )
    assert cp.resolve_org_token(second_raw) is None
    row = next(
        r for r in cp.get_connections(org)
        if r["avatar_id"] == "cedric" and r["provider"] == "cedric-brain"
    )
    assert row["status"] == "disconnected"
    assert row["config"]["install_tombstone"] is True
    assert row["config"]["revoked_install_nonce"] == "nonce-2"


def test_concurrent_same_state_postgres_cas_converges(cp):
    from concurrent.futures import ThreadPoolExecutor

    a = cp.ensure_user(
        "sub-install-race", "install-race@freemail.test", "Install Race", ""
    )
    org = a["org_id"]
    raw = "same-deterministic-token"
    assert cp.begin_brain_install(org, "cedric", "nonce-race", "") is True

    def accept(_):
        return cp.accept_brain_install(
            org, "cedric", "nonce-race", raw, "T_RACE", "",
            "race-secret", "race-peer",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(accept, range(8)))

    assert outcomes.count("applied") == 1
    assert outcomes.count("replay") == 7
    assert cp.resolve_org_token(raw) == org



def test_postgres_completion_disconnect_race_converges_to_tombstone(
    cp, monkeypatch
):
    """The production completion holds the org advisory lock through the
    external registry write; disconnect fences afterward and removes it."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.cedric import secret_registry

    user = cp.ensure_user(
        "sub-install-disconnect-race",
        "install-disconnect-race@freemail.test",
        "Install Disconnect Race",
        "",
    )
    org = user["org_id"]
    raw = "disconnect-race-deterministic-token"
    nonce = "nonce-disconnect-race"
    assert cp.begin_brain_install(org, "cedric", nonce, "") is True

    registry: dict[str, tuple[str, str]] = {}
    registry_entered = Event()
    release_registry = Event()
    disconnect_attempted = Event()

    def slow_upsert(target_org, secret, peer):
        registry[target_org] = (secret, peer)
        registry_entered.set()
        assert release_registry.wait(10)
        return True

    monkeypatch.setattr(secret_registry, "upsert_org_credentials", slow_upsert)

    def complete():
        return cp.complete_brain_install(
            org, "cedric", nonce, raw, "T_RACE", "",
            "race-secret", "race-peer",
        )

    def disconnect():
        disconnect_attempted.set()
        assert cp.begin_brain_disconnect(org, "cedric", "remote_revoked") is True
        registry.pop(org, None)
        assert cp.tombstone_brain_install(org, "cedric") is True
        return "disconnected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion_future = pool.submit(complete)
        assert registry_entered.wait(10)
        disconnect_future = pool.submit(disconnect)
        assert disconnect_attempted.wait(10)
        release_registry.set()
        assert completion_future.result(timeout=10) == "applied"
        assert disconnect_future.result(timeout=10) == "disconnected"

    assert registry == {}
    assert cp.resolve_org_token(raw) is None
    row = next(
        r for r in cp.get_connections(org)
        if r["avatar_id"] == "cedric" and r["provider"] == "cedric-brain"
    )
    assert row["status"] == "disconnected"
    assert row["config"]["install_tombstone"] is True


def test_begin_disconnect_immediately_revokes_only_own_org_token(cp):
    a = cp.ensure_user(
        "sub-disconnect-fence-a", "disconnect-fence-a@freemail.test", "A", ""
    )
    b = cp.ensure_user(
        "sub-disconnect-fence-b", "disconnect-fence-b@freemail.test", "B", ""
    )
    assert cp.set_connection(
        a["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T_A"}
    )
    assert cp.set_connection(
        b["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T_B"}
    )
    token_a = cp.rotate_org_token(a["org_id"], "cedric-slack-install")
    token_b = cp.rotate_org_token(b["org_id"], "cedric-slack-install")
    assert cp.resolve_org_token(token_a) == a["org_id"]
    assert cp.resolve_org_token(token_b) == b["org_id"]

    assert cp.begin_brain_disconnect(
        a["org_id"], "cedric", "revoke_pending"
    ) is True

    assert cp.resolve_org_token(token_a) is None
    assert cp.resolve_org_token(token_b) == b["org_id"]
    rows_a = cp.get_connections(a["org_id"])
    rows_b = cp.get_connections(b["org_id"])
    assert {row["status"] for row in rows_a} == {"disconnecting"}
    assert {row["status"] for row in rows_b} == {"connected"}


# ── production role + SECURITY DEFINER boundary + real RLS ──────────────

def test_runtime_engine_is_exact_policy_bound_role(cp):
    from sqlalchemy import text

    with cp._get_engine().connect() as conn:
        role = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
    assert role == (APP_ROLE, False, False)


def test_private_functions_have_exact_grants_and_identity_tables_are_hidden(cp, pg):
    # Signup succeeding above already proves laura_app can execute the definer.
    _signup_a(cp)
    functions = (
        "laura_private.ensure_user(text,text,text)",
        "laura_private.resolve_org_token(text)",
        "laura_private.list_open_usage_sessions()",
        "laura_private.claim_stripe_event(text,text)",
    )
    with _admin(pg) as conn:
        for function in functions:
            assert conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (APP_ROLE, function),
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT has_function_privilege('anon', %s, 'EXECUTE')",
                (function,),
            ).fetchone()[0] is False
            assert conn.execute(
                "SELECT has_function_privilege('authenticated', %s, 'EXECUTE')",
                (function,),
            ).fetchone()[0] is False

        # The deliberately pre-granted privileges in the fixture must be gone.
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'users', 'SELECT')", (APP_ROLE,)
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_table_privilege('anon', 'users', 'SELECT')"
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_table_privilege('authenticated', 'usage_sessions', 'SELECT')"
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'stripe_events', 'SELECT')", (APP_ROLE,)
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'org_tokens', 'INSERT')", (APP_ROLE,)
        ).fetchone()[0] is True
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'org_tokens', 'DELETE')", (APP_ROLE,)
        ).fetchone()[0] is True
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'org_tokens', 'SELECT')", (APP_ROLE,)
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_column_privilege(%s, 'org_tokens', 'org_id', 'SELECT')",
            (APP_ROLE,),
        ).fetchone()[0] is True
        assert conn.execute(
            "SELECT has_column_privilege(%s, 'org_tokens', 'label', 'SELECT')",
            (APP_ROLE,),
        ).fetchone()[0] is True
        assert conn.execute(
            "SELECT has_column_privilege(%s, 'org_tokens', 'token_hash', 'SELECT')",
            (APP_ROLE,),
        ).fetchone()[0] is False

    with _app(pg) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM users")
        # Only the two RLS routing columns are readable. Secrets and SELECT *
        # remain denied, while no-context routing reads return no tenant rows.
        assert conn.execute("SELECT org_id, label FROM org_tokens").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT token_hash FROM org_tokens")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM org_tokens")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM stripe_events")


def test_rls_isolates_reads_and_rejects_cross_org_write(cp, pg):
    a, b = _signup_a(cp), _signup_b(cp)
    assert cp.set_connection(
        a["org_id"], "laura", "calendar", "connected", {"who": "alice"}
    ) is True
    assert cp.set_connection(
        b["org_id"], "laura", "calendar", "connected", {"who": "bob"}
    ) is True
    assert [c["config"]["who"] for c in cp.get_connections(a["org_id"])] == ["alice"]

    with _app(pg) as conn:
        role = conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert role == (APP_ROLE, False, False)

        # No context: the policy's missing_ok setting resolves to no rows.
        assert conn.execute("SELECT count(*) FROM org_connections").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM billing_accounts").fetchone()[0] == 0

        # Org A context: WHERE-less reads can see only A.
        conn.execute("SELECT set_config('app.current_org', %s, false)", (a["org_id"],))
        got = {
            str(row[0])
            for row in conn.execute("SELECT org_id FROM org_connections").fetchall()
        }
        assert got == {a["org_id"]}
        got_billing = {
            str(row[0])
            for row in conn.execute("SELECT org_id FROM billing_accounts").fetchall()
        }
        assert got_billing == {a["org_id"]}

        # Even an explicit B org_id cannot cross the WITH CHECK boundary.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO org_connections "
                "(org_id, avatar_id, provider, status) "
                "VALUES (%s, 'laura', 'drive', 'connected')",
                (b["org_id"],),
            )

    with _admin(pg) as conn:
        leaked = conn.execute(
            "SELECT count(*) FROM org_connections "
            "WHERE org_id = %s AND provider = 'drive'",
            (b["org_id"],),
        ).fetchone()[0]
    assert leaked == 0

def test_owner_dsn_is_rejected_by_runtime_guard(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["admin_sa_url"])
    control_plane.reset_engine()
    with pytest.raises(RuntimeError, match="unsafe runtime database role"):
        control_plane._get_engine()
    assert control_plane._engine is None
    control_plane.reset_engine()



def test_lifespan_rejects_owner_dsn_before_background_start(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["admin_sa_url"])
    control_plane.reset_engine()
    try:
        with pytest.raises(RuntimeError, match="unsafe runtime database role") as exc:
            with TestClient(main.app):
                pass
        assert pg["admin_sa_url"] not in str(exc.value)
        assert control_plane._engine is None
    finally:
        control_plane.reset_engine()


def test_lifespan_rejects_unreachable_dsn_without_leaking_it(monkeypatch):
    secret = "startup-password-must-not-appear"
    dsn = f"postgresql://laura_app:{secret}@127.0.0.1:1/laura"
    monkeypatch.setattr(settings, "laura_database_url", dsn)
    control_plane.reset_engine()
    try:
        with pytest.raises(
            RuntimeError, match="runtime database role verification failed"
        ) as exc:
            with TestClient(main.app):
                pass
        rendered = str(exc.value)
        assert secret not in rendered
        assert dsn not in rendered
        assert "127.0.0.1" not in rendered
        assert control_plane._engine is None
    finally:
        control_plane.reset_engine()

def test_definer_owner_is_not_runtime_and_can_bypass_rls(pg):
    with _admin(pg) as conn:
        rows = conn.execute(
            "SELECT DISTINCT owner.rolname, owner.rolsuper, owner.rolbypassrls "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_roles owner ON owner.oid = p.proowner "
            "WHERE n.nspname = 'laura_private'"
        ).fetchall()
    assert rows
    assert all(name != APP_ROLE for name, _, _ in rows)
    assert all(is_super or bypasses for _, is_super, bypasses in rows)


def test_inactive_corporate_membership_cannot_regain_access(cp, pg):
    shared = str(uuid.uuid4())
    with _admin(pg) as conn:
        conn.execute(
            "INSERT INTO orgs (id, name, slug, plan) VALUES (%s, 'Locked', %s, 'free')",
            (shared, f"locked-{shared[:8]}"),
        )
        conn.execute(
            "INSERT INTO org_domains (org_id, domain, verified_at) "
            "VALUES (%s, 'locked.test', now())",
            (shared,),
        )
    first = cp.ensure_user("sub-locked", "employee@locked.test", "Emp", "")
    assert first["org_id"] == shared
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE memberships SET status = 'inactive' "
            "WHERE user_id = %s AND org_id = %s",
            (first["user_id"], shared),
        )

    with pytest.raises(Exception, match="corporate membership is not active"):
        cp.ensure_user("sub-locked", "employee@locked.test", "Emp", "")

    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT status FROM memberships WHERE user_id = %s AND org_id = %s",
            (first["user_id"], shared),
        ).fetchone() == ("inactive",)


def test_verified_domains_are_globally_unique_case_insensitive(pg):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with _admin(pg) as conn:
        conn.execute(
            "INSERT INTO orgs (id, name, slug, plan) VALUES "
            "(%s, 'Domain A', %s, 'free'), (%s, 'Domain B', %s, 'free')",
            (a, f"domain-a-{a[:8]}", b, f"domain-b-{b[:8]}"),
        )
        conn.execute(
            "INSERT INTO org_domains (org_id, domain, verified_at) "
            "VALUES (%s, 'CaseUnique.Test', now())",
            (a,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO org_domains (org_id, domain, verified_at) "
                "VALUES (%s, 'caseunique.test', now())",
                (b,),
            )


def test_pool_reuse_clears_transaction_local_org(cp):
    from sqlalchemy import text

    a = _signup_a(cp)
    engine = cp._get_engine()
    with engine.begin() as conn:
        cp._set_org(conn, a["org_id"])
        assert conn.execute(text("SELECT count(*) FROM billing_accounts")).scalar() == 1

    # A subsequent checkout may reuse the same physical connection. The
    # transaction-local GUC is empty; NULLIF(..., '') prevents an invalid UUID,
    # and FORCE RLS exposes no previous tenant.
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM billing_accounts")).scalar() == 0


def test_stripe_idempotency_uses_private_boundary(cp, pg):
    from sqlalchemy import text

    engine = cp._get_engine()
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT laura_private.claim_stripe_event(:id, :type)"),
            {"id": "evt-boundary", "type": "checkout.session.completed"},
        ).scalar() is True
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT laura_private.claim_stripe_event(:id, :type)"),
            {"id": "evt-boundary", "type": "checkout.session.completed"},
        ).scalar() is False

    with _app(pg) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM stripe_events")

