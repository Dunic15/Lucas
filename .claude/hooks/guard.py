#!/usr/bin/env python3
"""PreToolUse guard for the two rules that cost money or leak PII if broken.

Wired in .claude/settings.json. Receives the tool call as JSON on stdin.
Exit 0 = allow, exit 2 = block (stderr is shown to Claude as the reason).

Blocks:
  1. `git commit` when the STAGED diff (or the command itself) contains an
     API-key-shaped string  -> golden rule: no secrets in git.
  2. Edits/commits that add print/logger calls on transcript content
     -> golden rule: transcripts are PII, memory only, never logged.

Fail-open by design: if this guard crashes, the tool call proceeds; a broken
guard must not brick the workflow.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Concrete key shapes first (low false-positive), generic assignment last.
SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",                                   # AWS access key id
    r"\bgsk_[A-Za-z0-9]{20,}",                             # Groq
    r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}",            # OpenAI/Anthropic
    r"\bAIza[0-9A-Za-z_\-]{35}",                           # Google API key
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",  # private keys
    r"(?i)\b(?:api[_-]?key|client[_-]?secret|refresh[_-]?token|password)\b"
    r"['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{16,}['\"]",     # generic assignment
]
# print(...)/logger.x(...)/logging.x(...) with 'transcript' in the same call.
TRANSCRIPT_LOG = re.compile(
    r"(?i)\b(?:print|logger\.\w+|logging\.\w+)\s*\([^)\n]*transcript"
)


def block(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def scan_bash(command: str) -> None:
    for pat in SECRET_PATTERNS:
        if re.search(pat, command):
            block(
                "BLOCKED: this command contains an API-key-shaped string. "
                "Golden rule: no secrets in git or shell history; use .env "
                "(gitignored) or SSM /laura/prod/*."
            )
    if re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        try:
            diff = subprocess.run(
                ["git", "diff", "--cached", "-U0"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except Exception:
            return  # can't inspect -> allow
        added = "\n".join(l for l in diff.splitlines() if l.startswith("+"))
        for pat in SECRET_PATTERNS:
            m = re.search(pat, added)
            if m:
                block(
                    f"BLOCKED COMMIT: staged diff adds an API-key-shaped string "
                    f"('{m.group(0)[:12]}…'). Unstage it and move the secret to "
                    ".env (gitignored) or SSM. Golden rule: no secrets in git."
                )
        if TRANSCRIPT_LOG.search(added):
            block(
                "BLOCKED COMMIT: staged diff adds logging of transcript content "
                "(print/logger near 'transcript'). Transcripts are PII: memory "
                "only, never logged."
            )


def scan_edit(tool_input: dict) -> None:
    path = tool_input.get("file_path", "")
    # Only police files INSIDE this repo; scratchpads/temp files elsewhere are
    # not at risk of being committed or deployed.
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if path and not os.path.abspath(path).startswith(os.path.abspath(project) + os.sep):
        return
    if path.endswith((".md", ".txt")):  # docs can DISCUSS logging/keys freely
        return
    text = (tool_input.get("new_string") or "") + "\n" + (tool_input.get("content") or "")
    if TRANSCRIPT_LOG.search(text):
        block(
            "BLOCKED EDIT: this adds logging of transcript content "
            "(print/logger near 'transcript'). Transcripts are PII: memory "
            "only, never logged. Log counts/ids instead of content."
        )
    for pat in SECRET_PATTERNS[:5]:  # concrete key shapes only in code files
        if re.search(pat, text):
            block(
                "BLOCKED EDIT: content contains an API-key-shaped string. "
                "Keep secrets in .env (gitignored) or SSM /laura/prod/*."
            )


def main() -> None:
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if tool == "Bash":
        scan_bash(tool_input.get("command", ""))
    elif tool in ("Edit", "Write"):
        scan_edit(tool_input)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # fail-open: a broken guard must not brick the workflow
