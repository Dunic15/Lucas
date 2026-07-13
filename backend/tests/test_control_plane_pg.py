"""Two-org isolation on the REAL control plane — embedded Postgres (PR A).

Boots an embedded Postgres (pgserver), runs ``alembic upgrade head`` (0001 org
spine + 0002 control plane) against it, then proves on real RLS:

- two self-serve signups (control_plane.ensure_user) produce two DISTINCT UUID
  personal orgs, each with an owner membership, exactly the laura/cedric
  org_agents grants, and a free billing account (900 included seconds);
- signup is idempotent (re-login by sub OR by email returns the same ids);
- per-org machine tokens round-trip and NEVER cross orgs;
- FORCE RLS holds on the NEW 0002 tables for a NON-superuser policy-bound role
  (``laura_app``): with app.current_org set to org A, a bare SELECT on
  org_connections / billing_accounts returns ONLY org A's rows — and nothing
  at all without an org context.

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

from app import control_plane  # noqa: E402
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
    """Embedded Postgres with the FULL migration chain applied (0001 + 0002),
    plus the non-superuser app role the RLS assertions connect as."""
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
    sa_url = uri.replace("postgresql://", "postgresql+psycopg://")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_URL": sa_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    yield {"uri": uri, "sa_url": sa_url}
    control_plane.reset_engine()
    srv.cleanup()


@pytest.fixture
def cp(pg, monkeypatch):
    """control_plane pointed at the embedded PG (reset back afterwards so the
    rest of the suite stays key-free/disabled)."""
    monkeypatch.setattr(settings, "laura_database_url", pg["sa_url"])
    control_plane.reset_engine()
    yield control_plane
    control_plane.reset_engine()


def _signup_a(cp):
    return cp.ensure_user("sub-alice", "alice@freemail.test", "Alice", "")


def _signup_b(cp):
    return cp.ensure_user("sub-bob", "bob@freemail.test", "Bob", "")


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


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


# ── RLS on the NEW 0002 tables, as the policy-bound app role ───────────

def test_rls_isolates_new_tables_for_app_role(cp, pg):
    a, b = _signup_a(cp), _signup_b(cp)
    # seed one connection row per org through the DAL (also proves the
    # durable mirror round-trips its config)
    assert cp.set_connection(
        a["org_id"], "laura", "calendar", "connected", {"who": "alice"}
    ) is True
    assert cp.set_connection(
        b["org_id"], "laura", "calendar", "connected", {"who": "bob"}
    ) is True
    conns_a = cp.get_connections(a["org_id"])
    assert [c["config"]["who"] for c in conns_a] == ["alice"]

    with _admin(pg) as conn:
        conn.execute(
            f"GRANT SELECT ON org_connections, billing_accounts TO {APP_ROLE}"
        )

    import psycopg.conninfo as _ci

    app_kwargs = {
        **_ci.conninfo_to_dict(pg["uri"]),
        "user": APP_ROLE,
        "password": APP_ROLE_PASSWORD,
    }
    with psycopg.connect(**app_kwargs, autocommit=True) as conn:
        is_super = conn.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        assert not is_super, "app role must be policy-bound for this test to mean anything"

        # no org context → FORCE RLS yields nothing at all
        assert conn.execute("SELECT count(*) FROM org_connections").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM billing_accounts").fetchone()[0] == 0

        # as org A: a bare, WHERE-less SELECT sees ONLY org A's rows
        conn.execute("SELECT set_config('app.current_org', %s, false)", (a["org_id"],))
        got = {
            str(r[0])
            for r in conn.execute("SELECT org_id FROM org_connections").fetchall()
        }
        assert got == {a["org_id"]}
        got_billing = {
            str(r[0])
            for r in conn.execute("SELECT org_id FROM billing_accounts").fetchall()
        }
        assert got_billing == {a["org_id"]}
