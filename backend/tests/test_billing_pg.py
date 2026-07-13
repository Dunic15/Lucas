"""Real-Postgres money safety tests for Stripe billing migration 0005."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, entitlements  # noqa: E402
from app.config import settings  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_PASSWORD = "laura-billing-test-pw"


def _write_extension_shims():
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall" / "share" / "postgresql" / "extension"
    )
    shims = {
        "pgcrypto.control": "default_version = '1.0'\nrelocatable = true\n",
        "pgcrypto--1.0.sql": "-- gen_random_uuid is core\n",
        "citext.control": "default_version = '1.0'\nrelocatable = true\n",
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("billing_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE laura_app LOGIN PASSWORD '{APP_PASSWORD}' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        conn.execute("CREATE ROLE anon NOLOGIN")
        conn.execute("CREATE ROLE authenticated NOLOGIN")

    import psycopg.conninfo as ci
    from sqlalchemy.engine import URL

    info = ci.conninfo_to_dict(uri)
    query = {
        key: str(info[key])
        for key in ("host", "port")
        if info.get(key) is not None
    }
    admin_url = uri.replace("postgresql://", "postgresql+psycopg://")
    app_url = URL.create(
        "postgresql+psycopg",
        username="laura_app",
        password=APP_PASSWORD,
        database=info.get("dbname"),
        query=query,
    ).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={
            **os.environ,
            "LAURA_DATABASE_ADMIN_URL": admin_url,
            "LAURA_DATABASE_URL": "",
            "LAURA_REQUIRE_MIGRATIONS": "1",
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    )
    yield {"uri": uri, "app_url": app_url}
    control_plane.reset_engine()
    srv.cleanup()


@pytest.fixture
def cp(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_url"])
    monkeypatch.setattr(settings, "billing_checkout_reservation_seconds", 300)
    control_plane.reset_engine()
    yield control_plane
    control_plane.reset_engine()


def _org(cp, tag):
    return cp.ensure_user(
        f"sub-billing-{tag}",
        f"billing-{tag}@freemail.test",
        tag,
        "",
    )["org_id"]


def _bind(cp, org, tag):
    reservation = cp.reserve_checkout(org)
    assert reservation["ok"] is True
    customer = f"cus_{tag}"
    assert cp.bind_stripe_customer(
        org, reservation["revision"], customer
    ) is True
    return customer, reservation["revision"]


def _subscription(
    org,
    sub,
    *,
    created,
    status="active",
    access="active",
    valid=True,
    start=None,
    end=None,
):
    now = int(time.time())
    return {
        "kind": "subscription",
        "event_created": created,
        "asserted_org": org,
        "subscription_id": sub,
        "status": status,
        "access": access,
        "price_valid": valid,
        "period_start": start if start is not None else now - 60,
        "period_end": end if end is not None else now + 3600,
    }


def test_runtime_boundary_and_global_event_table(cp, pg):
    assert cp.billing_boundary_ready() is True
    with psycopg.connect(pg["app_url"]) as conn:
        role = conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert role == ("laura_app", False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM stripe_events")


def test_customer_binding_is_immutable_and_unique(cp):
    org_a = _org(cp, "immutable-a")
    org_b = _org(cp, "immutable-b")
    customer, revision = _bind(cp, org_a, "immutable")
    assert cp.bind_stripe_customer(org_a, revision, customer) is True
    assert cp.bind_stripe_customer(org_a, revision, "cus_rebind") is False
    reservation = cp.reserve_checkout(org_b)
    with pytest.raises(Exception):
        cp.bind_stripe_customer(
            org_b, reservation["revision"], customer
        )


def test_cancel_then_late_active_same_subscription_cannot_resurrect(cp):
    org = _org(cp, "sticky")
    customer, _ = _bind(cp, org, "sticky")
    assert cp.apply_stripe_event(
        "evt_sticky_active", "customer.subscription.created", customer,
        _subscription(org, "sub_sticky", created=100),
    ) is True
    assert cp.apply_stripe_event(
        "evt_sticky_cancel", "customer.subscription.deleted", customer,
        _subscription(
            org, "sub_sticky", created=200, status="canceled",
            access="terminal",
        ),
    ) is True
    assert cp.apply_stripe_event(
        "evt_sticky_late_active", "customer.subscription.updated", customer,
        _subscription(org, "sub_sticky", created=300),
    ) is True
    row = cp.get_billing(org)
    assert row["plan"] == "free"
    assert row["subscription_status"] == "canceled"


def test_old_subscription_cannot_cancel_new_subscription(cp):
    org = _org(cp, "old-sub")
    customer, _ = _bind(cp, org, "oldsub")
    cp.apply_stripe_event(
        "evt_old_active", "customer.subscription.created", customer,
        _subscription(org, "sub_old", created=100),
    )
    cp.apply_stripe_event(
        "evt_old_cancel", "customer.subscription.deleted", customer,
        _subscription(
            org, "sub_old", created=200, status="canceled",
            access="terminal",
        ),
    )
    cp.apply_stripe_event(
        "evt_new_active", "customer.subscription.created", customer,
        _subscription(org, "sub_new", created=300),
    )
    cp.apply_stripe_event(
        "evt_old_late_delete", "customer.subscription.deleted", customer,
        _subscription(
            org, "sub_old", created=400, status="canceled",
            access="terminal",
        ),
    )
    row = cp.get_billing(org)
    assert row["plan"] == "solo"
    assert row["stripe_subscription_id"] == "sub_new"
    assert row["subscription_status"] == "active"


def test_late_checkout_completion_never_grants(cp):
    org = _org(cp, "checkout-link")
    customer, _ = _bind(cp, org, "checkoutlink")
    cp.apply_stripe_event(
        "evt_link_active", "customer.subscription.created", customer,
        _subscription(org, "sub_link", created=100),
    )
    cp.apply_stripe_event(
        "evt_link_cancel", "customer.subscription.deleted", customer,
        _subscription(
            org, "sub_link", created=200, status="canceled",
            access="terminal",
        ),
    )
    cp.apply_stripe_event(
        "evt_late_checkout", "checkout.session.completed", customer,
        {
            "kind": "checkout_link",
            "event_created": 300,
            "asserted_org": org,
            "session_id": "cs_late",
            "subscription_id": "sub_link",
        },
    )
    row = cp.get_billing(org)
    assert row["plan"] == "free"
    assert row["subscription_status"] == "canceled"


def test_wrong_price_never_provisions(cp):
    org = _org(cp, "wrong-price")
    customer, _ = _bind(cp, org, "wrongprice")
    cp.apply_stripe_event(
        "evt_wrong_price", "customer.subscription.created", customer,
        _subscription(org, "sub_wrong", created=100, valid=False),
    )
    row = cp.get_billing(org)
    assert row["plan"] == "free"
    assert row["stripe_subscription_id"] is None


def test_invoice_is_current_subscription_only_and_period_monotonic(cp):
    org = _org(cp, "invoice")
    customer, _ = _bind(cp, org, "invoice")
    now = int(time.time())
    cp.apply_stripe_event(
        "evt_invoice_active", "customer.subscription.created", customer,
        _subscription(
            org, "sub_invoice", created=100,
            start=now - 100, end=now + 1000,
        ),
    )
    cp.apply_stripe_event(
        "evt_old_sub_invoice", "invoice.payment_failed", customer,
        {
            "kind": "invoice_failed",
            "event_created": 200,
            "subscription_id": "sub_old",
            "price_valid": True,
        },
    )
    assert cp.get_billing(org)["subscription_status"] == "active"

    cp.apply_stripe_event(
        "evt_invoice_failed", "invoice.payment_failed", customer,
        {
            "kind": "invoice_failed",
            "event_created": 300,
            "subscription_id": "sub_invoice",
            "price_valid": True,
        },
    )
    failed = cp.get_billing(org)
    assert failed["subscription_status"] == "past_due"
    assert failed["current_period_start"] == now - 100

    cp.apply_stripe_event(
        "evt_older_period_paid", "invoice.paid", customer,
        {
            "kind": "invoice_paid",
            "event_created": 400,
            "subscription_id": "sub_invoice",
            "price_valid": True,
            "period_start": now - 200,
            "period_end": now + 800,
        },
    )
    stale = cp.get_billing(org)
    assert stale["subscription_status"] == "past_due"
    assert stale["current_period_start"] == now - 100

    cp.apply_stripe_event(
        "evt_new_period_paid", "invoice.paid", customer,
        {
            "kind": "invoice_paid",
            "event_created": 500,
            "subscription_id": "sub_invoice",
            "price_valid": True,
            "period_start": now + 1000,
            "period_end": now + 2000,
        },
    )
    renewed = cp.get_billing(org)
    assert renewed["subscription_status"] == "active"
    assert renewed["current_period_start"] == now + 1000


def test_past_due_does_not_reset_and_expires_at_paid_through(cp, pg):
    org = _org(cp, "dunning")
    customer, _ = _bind(cp, org, "dunning")
    now = int(time.time())
    cp.apply_stripe_event(
        "evt_dunning_active", "customer.subscription.created", customer,
        _subscription(
            org, "sub_dunning", created=100,
            start=now - 100, end=now + 1000,
        ),
    )
    cp.apply_stripe_event(
        "evt_dunning_fail", "invoice.payment_failed", customer,
        {
            "kind": "invoice_failed",
            "event_created": 200,
            "subscription_id": "sub_dunning",
            "price_valid": True,
        },
    )
    assert entitlements.remaining_seconds(org) == int(
        settings.solo_included_seconds
    )
    with psycopg.connect(pg["uri"], autocommit=True) as conn:
        conn.execute(
            "UPDATE billing_accounts SET current_period_end = now() - "
            "interval '1 second' WHERE org_id = %s",
            (org,),
        )
    assert entitlements.remaining_seconds(org) == 0
    denied = entitlements.open_usage(org, "bot-expired", "laura")
    assert denied == {"ok": False, "reason": "usage_limit_reached"}


def test_paused_update_fails_closed(cp):
    org = _org(cp, "paused")
    customer, _ = _bind(cp, org, "paused")
    cp.apply_stripe_event(
        "evt_paused_active", "customer.subscription.created", customer,
        _subscription(org, "sub_paused", created=100),
    )
    cp.apply_stripe_event(
        "evt_paused", "customer.subscription.updated", customer,
        _subscription(
            org, "sub_paused", created=200, status="paused",
            access="terminal",
        ),
    )
    row = cp.get_billing(org)
    assert row["plan"] == "free"
    assert row["subscription_status"] == "paused"


def test_claim_rolls_back_when_tenant_write_fails(cp, monkeypatch):
    org = _org(cp, "rollback")
    customer, _ = _bind(cp, org, "rollback")
    effect = _subscription(org, "sub_rollback", created=100)
    original = cp._set_org

    def fail(*_args):
        raise RuntimeError("simulated tenant write failure")

    monkeypatch.setattr(cp, "_set_org", fail)
    with pytest.raises(RuntimeError):
        cp.apply_stripe_event(
            "evt_rollback", "customer.subscription.created", customer,
            effect,
        )
    monkeypatch.setattr(cp, "_set_org", original)
    assert cp.apply_stripe_event(
        "evt_rollback", "customer.subscription.created", customer,
        effect,
    ) is True


def test_concurrent_same_event_is_applied_once(cp):
    org = _org(cp, "concurrent-event")
    customer, _ = _bind(cp, org, "concurrentevent")
    effect = _subscription(org, "sub_concurrent", created=100)

    def apply():
        return cp.apply_stripe_event(
            "evt_concurrent", "customer.subscription.created",
            customer, effect,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(results) == [False, True]


def test_concurrent_checkout_has_one_reservation(cp):
    org = _org(cp, "concurrent-checkout")

    def reserve():
        return cp.reserve_checkout(org)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(bool(result and result.get("ok")) for result in results) == 1
    assert {result.get("reason") for result in results if not result.get("ok")} == {
        "checkout_in_progress"
    }


def test_member_role_uses_private_boundary(cp):
    signup = cp.ensure_user(
        "sub-billing-role", "billing-role@freemail.test", "role", ""
    )
    assert cp.member_role(signup["org_id"], signup["user_id"]) == "owner"

