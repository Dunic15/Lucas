# Can I build and test this for free?

**Yes; the brain and all the logic run 100% free.** Only the optional
"talking face in a real meeting" step needs paid/trial vendor accounts.

## The two modes

| Mode | What you can do | Cost |
|---|---|---|
| **Demo console** (default) | Ask avatars, get grounded cited answers, generate post-meeting checklists + emails | **Free**: runs offline |
| **Live meeting** | A talking avatar joins a real Zoom/Meet/Teams call | Needs Recall + Anam + ElevenLabs (trial/paid) |

## Per-service breakdown

> Free tiers and prices change; treat the "Free?" column as a starting point and
> confirm on each vendor's pricing page before you rely on it.

| Service | Role in the app | Free? (verify) |
|---|---|---|
| **Stub brain** (built in) | Deterministic offline reasoning; proves the pipeline | Free, no install |
| **Hash embeddings** (built in) | RAG retrieval, keyword-overlap vectors | Free, no install |
| **Ollama** (`llama3.2`) | Local LLM brain, real reasoning | Free, runs on your machine |
| **fastembed** (`EMBEDDING_PROVIDER=local`) | Real semantic embeddings, local | Free, `pip install fastembed` |
| **Anthropic Claude** | Best-quality brain | Paid per-token; no standing free tier |
| **Voyage AI** | Best-quality embeddings | Has a free token allowance, verify current limit |
| **Recall.ai** | Live meeting entry (ears + camera), required for the in-call agent | Paid (~per-hour); trial credits; verify |
| **Granola** | Post-meeting transcripts (summary/checklist path only; can't join calls) | Notetaker app; API key from the app, verify plan |
| **Anam** | Avatar face | Trial minutes, verify current allowance |
| **ElevenLabs** | Voice | Free monthly credit tier; verify |

## Recommended free stack

For everything except a live meeting:

```
BRAIN_PROVIDER=ollama        # or stub for zero install
EMBEDDING_PROVIDER=hash       # or local for better retrieval
```

This gives you real grounded answers and post-meeting artifacts at **zero cost**.
Add a single `ANTHROPIC_API_KEY` only when you want top-quality reasoning; add the
meeting vendors only for the final "face in a Zoom" demo.
