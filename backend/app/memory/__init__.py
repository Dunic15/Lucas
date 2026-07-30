"""Meeting Memory — the accumulating cross-meeting brain (Slice 1).

docs/company-brain/MEETING-MEMORY-SPEC.md. Distilled artifact fields only —
never a transcript, never an utterance. Flag-gated (MEETING_MEMORY_ENABLED)
and control-plane-gated; the key-free demo never touches any of it.
"""
from . import meeting_memory  # noqa: F401 — re-export for `memory.meeting_memory`
