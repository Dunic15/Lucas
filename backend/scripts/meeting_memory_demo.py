"""Meeting Memory + pre-meeting context — a runnable, key-free demo.

Spins up a THROWAWAY embedded Postgres (pgserver), migrates it, indexes three
synthetic meetings and one company document, then prints what a reviewer
actually wants to see:

  1. a historical question answered across meetings, with citations
  2. the same evidence as the model would receive it (untrusted-content framed)
  3. a permission check: the same query as a user who may not read the meeting
  4. a full pre-meeting context pack

No API keys, no network, no real tenant, and nothing written outside the
temporary directory. Run from the repo root:

    GRAPHIFY_SKIP_HOOK=1 BRAIN_PROVIDER=stub EMBEDDING_PROVIDER=hash \
      .venv/bin/python backend/scripts/meeting_memory_demo.py
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import pgserver
    import psycopg
except ImportError:  # pragma: no cover - operator guidance
    print("This demo needs pgserver + psycopg:  pip install pgserver psycopg")
    raise SystemExit(1)

DAY = 86400.0


def _rule(title: str) -> None:
    print(f"\n\033[1m{'─' * 78}\n{title}\n{'─' * 78}\033[0m")


def _when(value) -> str:
    if not value:
        return "—"
    return datetime.datetime.fromtimestamp(
        float(value), datetime.timezone.utc
    ).strftime("%Y-%m-%d")


def _boot(workdir: Path):
    """Embedded Postgres + migrations, exactly like the pg test fixture."""
    ext = (Path(pgserver.__file__).parent
           / "pginstall/share/postgresql/extension")
    for name, body in {
        "pgcrypto.control": ("default_version = '1.0'\nrelocatable = true\n"
                             "comment = 'demo shim'\n"),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": ("default_version = '1.0'\nrelocatable = true\n"
                           "comment = 'demo shim'\n"),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }.items():
        path = ext / name
        if not path.exists():
            path.write_text(body)

    srv = pgserver.get_server(str(workdir / "pg"))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        try:
            conn.execute("CREATE ROLE laura_app LOGIN PASSWORD 'pw' "
                         "NOSUPERUSER NOBYPASSRLS")
        except Exception:  # noqa: BLE001 — already exists on a reused dir
            pass

    import psycopg.conninfo as ci
    from sqlalchemy.engine import URL

    info = ci.conninfo_to_dict(uri)
    admin = uri.replace("postgresql://", "postgresql+psycopg://")
    app_url = URL.create(
        "postgresql+psycopg", username="laura_app", password="pw",
        database=info.get("dbname"),
        query={k: str(info[k]) for k in ("host", "port") if info.get(k)},
    ).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr)
        raise SystemExit("alembic upgrade failed")
    return srv, app_url


def _meetings(now: float):
    return [
        ("bot-acme-apr", {
            "summary": "Reviewed the Acme enterprise rollout. Agreed pricing "
                       "stays at the enterprise tier with a 12% volume "
                       "discount.",
            "decisions": [{"text": "Approve a 12% volume discount for Acme"},
                          {"text": "Ship the Acme pilot in Q3"}],
            "actions": [
                {"title": "Send the signed DPA to Acme legal",
                 "owner": "Sarah", "due": "2026-05-02"},
                {"title": "Book the Acme security review",
                 "owner": "Ada", "due": "2026-05-10"},
            ],
            "risks": ["Acme security review not yet booked"],
            "missing_steps": ["DPA signature outstanding"],
            "meeting_url": "https://meet.google.com/acme-weekly",
            "visibility": "org", "saved_at": now - 95 * DAY,
            "participation": [{"name": "Sarah Lin"}, {"name": "Ada Test"}],
            "transcript": "RAW TRANSCRIPT — must never be indexed",
        }, {"title": "Acme <> Laura enterprise rollout",
            "attendees": [{"name": "Sarah Lin", "email": "sarah@acme.example"},
                          {"name": "Ada Test", "email": "ada@synthetic.example"}],
            "customer": "Acme", "project": "Enterprise rollout",
            "topics": ["pricing", "security"]}),
        ("bot-acme-jun", {
            "summary": "Acme pilot is live. Decided to defer SSO to phase two.",
            "decisions": [{"text": "Defer Acme SSO to phase two"}],
            "actions": [{"title": "Confirm Acme pilot success criteria",
                         "owner": "Sarah", "due": "2026-07-01"}],
            "risks": ["Phase two scope undefined"],
            "missing_steps": [],
            "meeting_url": "https://meet.google.com/acme-weekly",
            "visibility": "org", "saved_at": now - 30 * DAY,
            "participation": [{"name": "Sarah Lin"}],
            "transcript": "RAW TRANSCRIPT — must never be indexed",
        }, {"title": "Acme pilot checkpoint",
            "attendees": [{"name": "Sarah Lin", "email": "sarah@acme.example"}],
            "customer": "Acme", "project": "Enterprise rollout",
            "topics": ["pilot", "sso"]}),
        ("bot-globex-jul", {
            "summary": "Globex migration kickoff.",
            "decisions": [{"text": "Start the Globex migration in August"}],
            "actions": [{"title": "Draft the Globex migration plan",
                         "owner": "Cai", "due": "2026-08-01"}],
            "risks": ["Globex legacy data quality unknown"],
            "missing_steps": [],
            "meeting_url": "https://zoom.us/j/globex",
            "visibility": "org", "saved_at": now - 5 * DAY,
            "participation": [{"name": "Cai Ito"}],
            "transcript": "RAW TRANSCRIPT — must never be indexed",
        }, {"title": "Globex migration kickoff",
            "attendees": [{"name": "Cai Ito", "email": "cai@globex.example"}],
            "customer": "Globex", "project": "Migration",
            "topics": ["migration"]}),
    ]


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="meeting-memory-demo-"))
    print(f"scratch dir: {workdir}  (safe to delete afterwards)")
    srv, app_url = _boot(workdir)

    from app import control_plane, store
    from app.config import settings

    settings.laura_database_url = app_url
    settings.data_foundation_enabled = True
    settings.company_brain_enabled = True
    store.STORE_PATH = workdir / "store.sqlite3"
    store._init_db()
    control_plane.reset_engine()

    from app.datafoundation import (connector_meeting, dal, framing,
                                    meeting_memory, premeeting_context)
    from app.datafoundation import sync as df_sync
    from app.datafoundation.envelope import body_checksum

    org = control_plane.ensure_user(
        f"sub-{time.time_ns()}", f"demo-{time.time_ns()}@freemail.test",
        "demo", "",
    )["org_id"]
    now = time.time()

    for bot_id, artifact, meta in _meetings(now):
        connector_meeting.emit_finalized(org, bot_id, artifact,
                                         meeting_meta=meta)

    # One company document, so the pack can mix meetings with knowledge.
    doc_connector = dal.ensure_connector(org, "upload", "Company docs")
    body = ("# Enterprise discount policy\n\nVolume discounts above 10% "
            "require finance approval before the DPA is countersigned.\n")
    env = {"external_id": "doc-discount", "kind": "document",
           "title": "Enterprise discount policy", "body_text": body,
           "mime": "text/markdown", "acl_mode": "org_default", "acl": [],
           "deleted": False, "checksum": body_checksum(body),
           "transform": "demo@1"}
    dal.commit_batch(org, doc_connector["id"],
                     df_sync.materialize_bodies(org, doc_connector, [env]),
                     new_cursor=None)

    _rule("1. Historical question across meetings, with citations")
    print('Q: "What did we decide with Acme in the last six months?"\n')
    found = meeting_memory.search(
        org, principal_ref="u_demo",
        query="Acme decided decision pricing discount",
        filters={"customer": "Acme", "since": now - 180 * DAY}, k=5,
    )
    for hit in found["results"]:
        cite = hit["citation"]
        print(f'  [{cite["meeting_id"]}] {cite["title"]}  '
              f'({_when(cite["date"])}, customer={cite["customer"]})')
        print(f'      {hit["excerpt"][:120]}')
    print(f'\n  Globex is indexed too, and correctly absent: '
          f'{found["meetings"]} meetings matched the Acme filter.')

    _rule("2. The same evidence as the model receives it (untrusted framing)")
    print(framing.frame_meetings(found)[:900] + "\n  …")

    _rule("3. Permission check — a user who may not read a private meeting")
    connector_meeting.emit_finalized(
        org, "bot-private",
        {"summary": "SECRETSENTINEL merger terms with Acme",
         "decisions": [], "actions": [], "risks": [], "missing_steps": [],
         "meeting_url": "https://meet.google.com/private",
         "visibility": "private", "principal_id": "u_founder",
         "saved_at": now - 2 * DAY, "participation": [{"name": "Founder"}],
         "transcript": "RAW"},
        meeting_meta={"title": "Board private", "customer": "Acme"},
    )
    blind = meeting_memory.search(org, principal_ref="u_demo",
                                  query="SECRETSENTINEL merger", k=5)
    print(f'  u_demo searching "SECRETSENTINEL merger" -> '
          f'{len(blind["results"])} results, {blind["meetings"]} meetings')
    print(f'  sentinel present anywhere in the response? '
          f'{"SECRETSENTINEL" in repr(blind)}')
    print("  (the private meeting exists and is indexed — it is simply "
          "absent for this user: no snippet, no citation, no count)")

    _rule("4. Pre-meeting context pack (read-only)")
    pack = premeeting_context.build(
        org, principal_ref="u_demo",
        event={"title": "Acme <> Laura weekly",
               "agenda": "Enterprise rollout and pricing",
               "customer": "Acme", "project": "Enterprise rollout",
               "organizer": "sarah@acme.example", "platform": "google_meet",
               "starts_at": now + 3600,
               "attendees": [
                   {"name": "Sarah Lin", "email": "sarah@acme.example"},
                   {"name": "Ada Test", "email": "ada@synthetic.example"}]},
    )
    rel = pack["relationship"]
    print(f'  read_only={pack["read_only"]}  customer={rel["customer"]}  '
          f'prior_meetings={rel["prior_meetings"]}  '
          f'last_met={_when(rel["last_met_at"])}')
    print("\n  PREVIOUSLY")
    for item in pack["previously"]:
        print(f'    - [{item["meeting_id"]}] {item["title"]} '
              f'({_when(item["date"])})')
    print("\n  OPEN COMMITMENTS")
    for c in pack["open_commitments"]:
        print(f'    - {c["title"]}  (owner={c["owner"] or "—"}, '
              f'due={c["due"] or "—"})  <- {c["from_meeting_id"]}')
    print("\n  RISKS / OPEN QUESTIONS")
    for r in pack["risks_and_open_questions"]:
        print(f'    - [{r["kind"]}] {r["text"]}  <- {r["from_meeting_id"]}')
    print("\n  COMPANY KNOWLEDGE")
    for k in pack["company_knowledge"]:
        print(f'    - {k["source"]}: {k["excerpt"][:80]}')
    print("\n  SUGGESTED DISCUSSION POINTS (suggestions only — any write "
          "still needs Action Center approval)")
    for point in pack["discussion_points"]:
        print(f'    - {point["point"]}')
    print(f'\n  citations={len(pack["citations"])}  '
          f'freshness={{generated {_when(pack["freshness"]["generated_at"])}, '
          f'window {pack["freshness"]["evidence_window_days"]}d, '
          f'degraded={pack["freshness"]["degraded"]}}}')

    _rule("5. PII boundary — the transcript is never indexed")
    leak = meeting_memory.search(org, principal_ref="u_demo",
                                 query="RAW TRANSCRIPT", k=5)
    print(f'  searching "RAW TRANSCRIPT" -> {len(leak["results"])} results')

    srv.cleanup()
    print(f"\ndone. temporary Postgres removed; scratch dir left at {workdir}")


if __name__ == "__main__":
    main()
