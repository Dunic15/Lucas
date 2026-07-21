"""Reasoning + retrieval domain: engine (the answer engine, formerly brain.py),
llm (provider clients), rag + embeddings (grounding), tools + tool_registry
(live action-capture tools). DOCSTRING-ONLY __init__ on purpose: a re-export
cannot preserve the engine's module identity, which tests monkeypatch -
consumers import app.brain.engine explicitly. Sibling submodules keep compat
shims at their old flat paths.
"""
