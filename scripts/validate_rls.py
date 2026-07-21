"""Validate the RLS ISOLATION MECHANISM the tenancy migration relies on, on REAL
Postgres (embedded pgserver). The full Alembic 0001 needs pgcrypto+citext (Supabase
has them; embedded PG doesn't), so we apply the migration's EXACT RLS DDL pattern
(FORCE ROW LEVEL SECURITY + policy on current_setting('app.current_org')) to a
test table and prove org B cannot read/write org A's rows as the NON-superuser
laura_app role. This is the safety thesis (MULTI-TENANCY.md §1/§4)."""
import tempfile, uuid, sys
import pgserver, psycopg

srv = pgserver.get_server(tempfile.mkdtemp(prefix="rls_"))
uri = srv.get_uri()
fails = []

# superuser: role + table + the migration's EXACT RLS pattern
with psycopg.connect(uri, autocommit=True) as c:
    c.execute("CREATE ROLE laura_app LOGIN PASSWORD 'pw' NOSUPERUSER")
    c.execute("CREATE TABLE t (org_id uuid NOT NULL, data text)")
    # ↓ verbatim from 0001_org_id_spine.py (RLS section)
    c.execute("ALTER TABLE t ENABLE ROW LEVEL SECURITY")
    c.execute("ALTER TABLE t FORCE  ROW LEVEL SECURITY")
    c.execute("""CREATE POLICY tenant_isolation ON t
                   USING      (org_id = current_setting('app.current_org', true)::uuid)
                   WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)""")
    c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON t TO laura_app")
    A, B = str(uuid.uuid4()), str(uuid.uuid4())

import psycopg.conninfo as _ci
_app_kw = {**_ci.conninfo_to_dict(uri), "user": "laura_app", "password": "pw"}
with psycopg.connect(**_app_kw, autocommit=True) as c:
    su = c.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    if su:
        fails.append("laura_app is SUPERUSER: FORCE RLS test invalid")
    for org in (A, B):
        c.execute("SELECT set_config('app.current_org', %s, false)", (org,))
        c.execute("INSERT INTO t (org_id, data) VALUES (%s, %s)", (org, f"secret-{org[:8]}"))
    # as B: a BARE SELECT (a "forgotten WHERE") must return ONLY B's row
    c.execute("SELECT set_config('app.current_org', %s, false)", (B,))
    got = {r[0] for r in c.execute("SELECT org_id::text FROM t").fetchall()}
    print(f"as org B, bare `SELECT * FROM t` → orgs visible: {got}")
    if got != {B}:
        fails.append(f"RLS LEAK: org B saw {got}, expected only B")
    # cross-org WRITE must be rejected by WITH CHECK
    try:
        c.execute("INSERT INTO t (org_id, data) VALUES (%s,'evil')", (A,))
        fails.append("WITH CHECK bypass: org B wrote a row stamped org A")
    except psycopg.errors.Error:
        print("as org B, INSERT stamped org A → rejected by WITH CHECK ✅")
    # cross-org UPDATE reaches 0 rows (can't even see A's row to update it)
    c.execute("UPDATE t SET data='hacked'")
    n = c.execute("SELECT count(*) FROM t WHERE data='hacked'").fetchone()[0]
    print(f"as org B, `UPDATE t SET data='hacked'` (no WHERE) touched {n} row(s). B's only")

srv.cleanup()
if fails:
    print("\n❌ RLS FAILED:"); [print("  -", f) for f in fails]; sys.exit(1)
print("\n✅ RLS SAFETY THESIS VALIDATED on real Postgres: FORCE row-level security "
      "isolates org B from org A for a non-superuser even on a forgotten WHERE, and "
      "WITH CHECK blocks cross-org writes; the migration's exact isolation pattern holds.")
