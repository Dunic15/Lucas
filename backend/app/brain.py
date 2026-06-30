"""The reasoning layer (Claude).

Two jobs:
  1. answer_question()  — live, grounded, cited answers when the avatar is called.
  2. post_meeting()     — summary + gap checklist + draft follow-up email.

Everything is grounded in retrieved process docs. The model is instructed to
say so when context is insufficient, and to return a confidence the speak-gate
can threshold on. That confidence + citation pair is the trust layer.
"""
from __future__ import annotations

import json

from anthropic import Anthropic

from .config import settings
from .rag import retrieve, Retrieved

_client: Anthropic | None = None


def _anthropic() -> Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _format_context(chunks: list[Retrieved]) -> str:
    blocks = []
    for i, c in enumerate(chunks, 1):
        blocks.append(f"[{i}] (source: {c.source} — {c.section})\n{c.text}")
    return "\n\n".join(blocks)


ANSWER_SYSTEM = """You are a callable AI process expert that has been invited \
into a live work meeting. You speak ONLY from the company process documents \
provided as context. You are concise: this is spoken aloud, so answer in 1-3 \
short sentences a person can absorb by ear.

Rules:
- Use ONLY the provided context. Do not invent steps, owners, or approvals.
- If the context does not contain the answer, say so plainly and do not guess.
- Cite the source document you relied on.
- Spoken style: no markdown, no bullet symbols, no headings.

Return ONLY a JSON object:
{
  "answer": "<what the avatar should say, spoken style>",
  "citations": ["<source filename>", ...],
  "confidence": <0.0-1.0, how well the context supports this answer>,
  "sufficient_context": <true|false>
}"""


def answer_question(question: str, *, k: int = 4) -> dict:
    """Retrieve + answer. Returns dict with answer/citations/confidence."""
    chunks = retrieve(question, k=k)
    context = _format_context(chunks)

    msg = _anthropic().messages.create(
        model=settings.brain_model,
        max_tokens=400,
        system=ANSWER_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Company process context:\n\n{context}\n\n"
                    f"Someone in the meeting asked:\n{question}\n\n"
                    "Respond with the JSON object only."
                ),
            }
        ],
    )
    result = _parse_json(msg.content[0].text)
    result.setdefault("citations", [c.source for c in chunks[:1]])
    result.setdefault("confidence", 0.0)
    result.setdefault("sufficient_context", False)
    result["retrieved"] = [
        {"source": c.source, "section": c.section, "score": round(c.score, 3)}
        for c in chunks
    ]
    return result


POSTMEETING_SYSTEM = """You analyze a meeting transcript against company \
process knowledge. Produce a crisp post-meeting artifact a team can act on.

Detect PROCESS GAPS, specifically any of: missing owner, missing deadline, \
missing approval, missing required document, unresolved blocker. Only flag a \
gap if it is genuinely implied by the discussion; do not pad the list.

Return ONLY a JSON object:
{
  "summary": "<3-5 sentence plain summary of what was discussed and decided>",
  "checklist": [
    {"item": "<action>", "owner": "<name or 'UNASSIGNED'>", "gap_type": "<owner|deadline|approval|document|blocker|none>"}
  ],
  "follow_up_email": {
    "subject": "<subject line>",
    "body": "<short professional email body summarizing decisions and next steps>"
  }
}"""


def post_meeting(transcript_text: str, *, k: int = 6) -> dict:
    """Summary + gap checklist + draft follow-up email for a finished meeting."""
    # Ground gap-detection in the actual process docs.
    chunks = retrieve(transcript_text[-3000:] or "process steps owners approvals", k=k)
    context = _format_context(chunks)

    msg = _anthropic().messages.create(
        model=settings.brain_model,
        max_tokens=1200,
        system=POSTMEETING_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Relevant company process context:\n\n{context}\n\n"
                    f"Meeting transcript:\n\n{transcript_text}\n\n"
                    "Respond with the JSON object only."
                ),
            }
        ],
    )
    return _parse_json(msg.content[0].text)


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction — strips ``` fences and surrounding prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"answer": text, "confidence": 0.0, "sufficient_context": False}
