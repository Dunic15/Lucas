"""Adversarial checks: no secrets, no real data, deterministic fixtures.

These are the safety gates for a synthetic demo pack. They scan every file the
pack ships (excluding this tests/ directory's own denylist source, which
naturally contains the patterns it hunts for)."""
from __future__ import annotations

import re
from pathlib import Path

# Secret-shaped patterns (the guard also lives in the repo's commit hook; this
# is the pack-local version so a bad string fails a focused test too).
SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "PEM private key"),
    (r"AIza[0-9A-Za-z_\-]{35}", "Google API key"),
    (r"xox[baprs]-[0-9A-Za-z-]{10,}", "Slack token"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key|client[_-]?secret)\b\s*[:=]\s*"
     r"['\"][^'\"]{8,}['\"]", "inline credential assignment"),
]

# Real-data markers that must never appear in a synthetic pack.
REAL_DATA_DENYLIST = [
    "sff.vc", "@sff", "SFF Studio", "lauravatar", "meet-cedric",
    "readyplayer", "avaturn", "duccio", "@gmail.com", "awsapprunner",
]

# Fixtures must be deterministic; no wall-clock / randomness in product code.
NONDETERMINISM = [r"\brandom\.", r"\buuid\b", r"\btime\.time\b",
                  r"datetime\.now", r"date\.today", r"Math\.random"]

DEMO = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()


def _pack_files() -> list[Path]:
    out = []
    for p in DEMO.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in {".pyc"} or "__pycache__" in p.parts:
            continue
        if p == THIS:                 # this file legitimately holds the patterns
            continue
        out.append(p)
    return out


def test_no_secret_shaped_strings():
    hits = []
    for p in _pack_files():
        text = p.read_text(errors="ignore")
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, text):
                hits.append(f"{p.relative_to(DEMO)}: {label}")
    assert not hits, f"secret-shaped strings: {hits}"


def test_no_real_data_markers():
    hits = []
    for p in _pack_files():
        low = p.read_text(errors="ignore").lower()
        for marker in REAL_DATA_DENYLIST:
            if marker.lower() in low:
                hits.append(f"{p.relative_to(DEMO)}: {marker}")
    assert not hits, f"real-data markers: {hits}"


def test_no_email_addresses_in_pack():
    email = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    hits = []
    for p in _pack_files():
        for m in email.findall(p.read_text(errors="ignore")):
            hits.append(f"{p.relative_to(DEMO)}: {m}")
    assert not hits, f"email addresses present: {hits}"


def _code_without_strings_and_comments(src: str) -> str:
    """Return only the executable tokens, strings and comments dropped, so a
    docstring that *mentions* time.time() to explain the code is deterministic
    doesn't read as a nondeterministic call."""
    import io
    import tokenize

    parts = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT,
                            tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT):
                continue
            parts.append(tok.string)
    except tokenize.TokenError:
        return src  # fall back to raw scan rather than pass silently
    return " ".join(parts)


def test_product_code_is_deterministic():
    hits = []
    for p in (DEMO / "product").glob("*.py"):
        code = _code_without_strings_and_comments(p.read_text())
        for pat in NONDETERMINISM:
            if re.search(pat, code):
                hits.append(f"{p.name}: {pat}")
    assert not hits, f"nondeterministic constructs in product code: {hits}"


def test_every_knowledge_doc_declares_synthetic():
    for p in (DEMO / "knowledge").glob("*.md"):
        assert "SYNTHETIC" in p.read_text(), p.name
