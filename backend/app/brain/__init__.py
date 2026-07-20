"""Reasoning + retrieval domain: engine (the answer engine, formerly brain.py),
llm (provider clients), rag + embeddings (grounding/retrieval), tools +
tool_registry (live action-capture tools). The engine module is named `engine`
to avoid a clash with this `brain/` package; consumers import it explicitly as
app.brain.engine. Sibling submodules keep compat shims at their old app/ paths.
"""
