"""Self-knowledge split: about/ docs answer self-questions and NEVER pollute
process retrieval. No vendors/keys — hash embeddings + the repo's own docs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.brain import engine as brain  # noqa: E402
from app.rag import retrieve, retrieve_about  # noqa: E402


def _laura():
    return avatars.load("laura")


def test_about_intent_detection():
    yes = [
        "how do you work?",
        "What can you do?",
        "who built you?",
        "are you an AI?",
        "what's your architecture like",
        "what model are you built on?",
        "are you built with Groq?",
        "what LLM are you using?",
        "Laura come funzioni?",
        "cosa sai fare?",
        "che modello usi?",
        "su che tecnologia sei fatta?",
        "chi sei?",
        "can you browse the web?",
        "are you able to look things up on the internet?",
        "could you search the web?",
        "puoi navigare sul web?",
        "sai cercare su internet?",
        "what does your web browsing cost?",
        "how does your browser work?",
        # Tool-roster questions (live gap 2026-07-21: "quali tool puoi
        # usare?" bypassed the about/ playbooks entirely)
        "what tools can you use?",
        "which integrations do you have?",
        "what apps can you access?",
        "quali tool puoi usare?",
        "che strumenti hai?",
        "quali integrazioni puoi usare?",
    ]
    no = [
        "what are we missing before go-live?",
        "who approves the security review?",
        "what's your take on the pricing?",  # opinion ask, not a self-question
        "can you check the deadline?",
        "come funziona l'onboarding?",  # about OUR process, not about her
        "search the web for the latest Acme funding news",  # task, not self-Q
        "look up the vendor pricing online and summarize it",  # task
        # a REQUEST with an object is a search, not a capability question
        # (2026-07-24: "can you search that up?" got a promise, never a search)
        "Can you browse the web and look something up online for us?",
        "Can you search that up on the Internet?",
        "can you check the onboarding checklist?",  # no web word
        "use the tool to file the ticket",  # task with a tool word, not self-Q
        "usa il tool per creare il task",  # task (IT), not self-Q
    ]
    for q in yes:
        assert brain._is_about_avatar(q), q
    for q in no:
        assert not brain._is_about_avatar(q), q


def test_process_retrieval_never_returns_meta_docs():
    """The regression that motivated the split: a real process question used to
    pull Laura's own playbook/architecture docs."""
    hits = retrieve(_laura(), "what are we missing before go-live?", k=4)
    assert hits, "process docs must be indexed"
    meta = {
        "answer_quality_playbook.md",
        "laura_architecture.md",
        "live_meeting_runbook.md",
        "provider_cost_and_replacement.md",
    }
    assert not ({h.source for h in hits} & meta)


def test_about_retrieval_hits_meta_docs():
    hits = retrieve_about(_laura(), "how do you work? what is your architecture?", k=4)
    assert hits, "about docs must be indexed"
    assert any(h.source == "laura_architecture.md" for h in hits)


def test_about_retrieval_empty_for_avatar_without_about_dir():
    sff = avatars.load("sff")
    assert retrieve_about(sff, "how do you work?", k=4) == []


def test_retrieve_for_routes_by_intent():
    laura = _laura()
    about_hits = brain._retrieve_for(laura, "how do you work?", "", 4)
    process_hits = brain._retrieve_for(laura, "what accounts does a new hire need?", "", 4)
    assert any("laura" in h.source or "playbook" in h.source or "runbook" in h.source
               or "architecture" in h.source or "provider" in h.source for h in about_hits)
    assert all(h.source in {"onboarding_sop.md", "access_security_sop.md"} for h in process_hits)
