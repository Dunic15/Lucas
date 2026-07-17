"""Company Brain ingestion — extraction, chunking, jobs, and the index bridge.

The worker tick (main.py lifespan loop, NEVER the live path) claims due
knowledge_sync_jobs per org with the SKIP LOCKED + lease pattern and runs:

  ingest_document — raw bytes → extract → cap → checksum → transactional
                    publish (version + chunks) → enqueue an index rebuild.
  rebuild_index   — Postgres chunks → rag.build_org_index_from_chunks per
                    assigned avatar; the in-memory index files this writes
                    are what rag.retrieve(org_id=...) merges live.
  sync_drive      — list an org's Drive folder with ITS OWN OAuth token
                    (google_client org tokens; requires the drive.readonly
                    scope — a clear per-job error tells the operator to
                    reconnect Google with that scope, never a silent skip).

Extraction reuses rag.py's chunkers so Company Brain retrieval behaves
exactly like base-pack retrieval. Documents are UNTRUSTED CONTENT: text is
chunked and cited, never treated as instructions, and never logged.
"""
from __future__ import annotations

import hashlib
import io
from typing import Any

import httpx

from .. import avatars, rag
from ..config import settings
from . import dal, storage

_TEXT_SUFFIXES = (".md", ".txt", ".markdown")
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_EXPORT_MIMES = {"application/vnd.google-apps.document": "text/plain"}
_DRIVE_MAX_FILES = 25


def extract_text(filename: str, data: bytes) -> str:
    """Extracted text for one uploaded file (md/txt/pdf/docx). Raises
    RuntimeError with a distilled reason for unsupported/broken input."""
    name = (filename or "").lower()
    if name.endswith(_TEXT_SUFFIXES) or not name.rsplit(".", 1)[-1:]:
        return data.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("pdf extraction needs pypdf") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"pdf unreadable ({type(e).__name__})") from e
    if name.endswith(".docx"):
        try:
            import docx  # python-docx, optional
        except ImportError as e:
            raise RuntimeError("docx extraction needs python-docx") from e
        try:
            document = docx.Document(io.BytesIO(data))
            return "\n\n".join(p.text for p in document.paragraphs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"docx unreadable ({type(e).__name__})") from e
    # Default: treat as UTF-8 text — covers extensionless pastes and .csv-ish
    # content without pretending to parse binaries.
    if b"\x00" in data[:1024]:
        raise RuntimeError("unsupported binary file type")
    return data.decode("utf-8", errors="replace")


def chunk_text(filename: str, text: str) -> list[dict[str, Any]]:
    """rag.py chunking (headings + windows) as plain dicts for the DAL."""
    name = (filename or "document").rsplit("/", 1)[-1]
    if name.lower().endswith((".md", ".markdown")):
        chunks = rag._chunk_markdown(text, name)
    else:
        chunks = rag._chunk_plain(text, name)
    return [
        {"text": c.text, "source": c.source, "section": c.section}
        for c in chunks
    ]


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ingest_document(org_id: str, document_id: str) -> None:
    """Extract → cap → publish one document (raises on failure; the caller
    records the distilled error on the job AND the document)."""
    doc = dal.get_document(org_id, document_id)
    if doc is None:
        raise RuntimeError("document missing")
    data = storage.get_bytes(doc["storage_ref"])
    if data is None:
        raise RuntimeError("stored bytes unavailable")
    text = extract_text(doc["filename"], data)
    text = text[: settings.knowledge_max_extracted_chars]
    if not text.strip():
        raise RuntimeError("no extractable text")
    chunks = chunk_text(doc["filename"], text)
    result = dal.publish_version(
        org_id, document_id, checksum=_checksum(text), text_content=text,
        chunks=chunks,
    )
    if result.get("error"):
        raise RuntimeError(str(result["error"]))


def _avatars_with_index_files(org_id: str) -> set[str]:
    """Avatar ids that currently have an on-disk org index file — they must
    be swept even when their assignments vanished (a deleted source's stale
    index would otherwise keep serving removed content forever)."""
    slug = rag._org_slug(org_id)
    try:
        from .. import store

        base = store.STORE_PATH.parent / "org_indexes"
        return {
            p.name[len(slug) + 2: -len(".index.json")]
            for p in base.glob(f"{slug}__*.index.json")
        }
    except OSError:
        return set()


def rebuild_indexes(org_id: str) -> int:
    """Regenerate every relevant avatar's per-org index file from Postgres.

    Covers the UNION of currently assigned avatars and avatars whose index
    file exists on disk: an avatar with zero remaining chunks (source deleted
    or unassigned) gets its file REMOVED — retrievability ends with the
    assignment, not with the next deploy."""
    targets = set(dal.assigned_avatars(org_id)) | _avatars_with_index_files(org_id)
    total = 0
    for avatar_id in sorted(targets):
        try:
            avatar = avatars.load(avatar_id)
        except Exception:  # noqa: BLE001 — an uninstalled avatar id
            continue
        chunks = dal.chunks_for_avatar(org_id, avatar_id)
        total += rag.build_org_index_from_chunks(avatar, org_id, chunks)
    return total


def sync_drive(org_id: str, source_id: str) -> int:
    """Pull an org's Drive folder with the org's OWN Google token.

    Requires the drive.readonly scope on the org's OAuth grant — the same
    token machinery the native executor uses (google_client), never the
    global inbox token that drive_client.folder_brief uses. A 403 becomes a
    clear reconnect instruction on the job."""
    source = dal.get_source(org_id, source_id)
    if source is None or source["kind"] != "drive":
        raise RuntimeError("drive source missing")
    folder = str(source.get("drive_folder_id") or "").strip()
    if not folder:
        raise RuntimeError("source has no drive_folder_id")
    from .. import google_client

    token, err = google_client._access_token(org_id)
    if not token:
        raise RuntimeError(f"google not connected ({err})")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(
            _DRIVE_FILES_URL,
            params={
                "q": f"'{folder}' in parents and trashed=false",
                "fields": "files(id,name,mimeType,size,modifiedTime)",
                "orderBy": "modifiedTime desc",
                "pageSize": _DRIVE_MAX_FILES,
            },
            headers=headers,
            timeout=20.0,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"drive list failed ({type(e).__name__})") from e
    if resp.status_code == 403:
        raise RuntimeError(
            "drive scope missing — reconnect Google including drive.readonly"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"drive list HTTP {resp.status_code}")
    ingested = 0
    for f in resp.json().get("files", []) or []:
        mime = str(f.get("mimeType") or "")
        if mime.startswith("application/vnd.google-apps"):
            export = _DRIVE_EXPORT_MIMES.get(mime)
            if not export:
                continue  # sheets/slides: out of scope for v1
            url = f"{_DRIVE_FILES_URL}/{f['id']}/export"
            params = {"mimeType": export}
            filename = f"{f.get('name') or f['id']}.txt"
        else:
            url = f"{_DRIVE_FILES_URL}/{f['id']}"
            params = {"alt": "media"}
            filename = str(f.get("name") or f["id"])
        try:
            body = httpx.get(
                url, params=params, headers=headers, timeout=30.0
            )
            if body.status_code != 200:
                continue
            data = body.content[: settings.knowledge_max_file_bytes]
        except Exception:  # noqa: BLE001 — skip one file, keep the sync
            continue
        doc = dal.upsert_document(
            org_id, source_id, filename, mime=mime, size_bytes=len(data),
        )
        if doc is None:
            continue
        ref = storage.put_bytes(org_id, doc["id"], filename, data)
        dal.set_document_storage(org_id, doc["id"], ref)
        dal.enqueue_job(org_id, source_id, "ingest_document", doc["id"])
        ingested += 1
    return ingested


def process_due(max_orgs: int = 5, jobs_per_org: int = 4) -> int:
    """One worker tick: claim and run due jobs. Returns jobs handled."""
    handled = 0
    for org_id in dal.due_orgs(max_orgs):
        for job in dal.claim_due_jobs(org_id, jobs_per_org):
            ok, error = True, ""
            try:
                if job["kind"] == "ingest_document":
                    ingest_document(org_id, job["document_id"])
                    dal.enqueue_job(
                        org_id, job["source_id"], "rebuild_index"
                    )
                elif job["kind"] == "rebuild_index":
                    rebuild_indexes(org_id)
                elif job["kind"] == "sync_drive":
                    sync_drive(org_id, job["source_id"])
            except Exception as e:  # noqa: BLE001 — distilled reason only
                ok, error = False, f"{type(e).__name__}: {e}"[:200]
                if job["kind"] == "ingest_document" and job["document_id"]:
                    try:
                        dal.mark_document_failed(
                            org_id, job["document_id"], error
                        )
                    except Exception:  # noqa: BLE001
                        pass
            dal.finish_job(
                org_id, job["id"], job["lease_token"], ok=ok, error=error,
                attempts=int(job["attempts"]),
            )
            handled += 1
    return handled
