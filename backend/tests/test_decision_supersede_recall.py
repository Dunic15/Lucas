"""The supersede link must survive how people ACTUALLY revise a decision.

Live gap 2026-07-22: the owner said "this substitute the decision of before"
and the pill never appeared. Two independent causes, both covered here:

  1. The cue list only knew formal register (supersedes / instead of /
     no longer). Natural revisions — "substitutes", "switching to", "scrap
     that", "changed our mind", and the Italian the owner's meetings switch
     into — drew no link.
  2. Even with a perfect cue, the summarizer NORMALIZES a decision into clean
     prose ("use construction as the primary niche"), so the spoken revision
     wording is often gone by the time the deterministic linker reads the text.
     The summarizer's own supersede signal was being dropped on the floor.

WHICH decision gets superseded is still resolved against real prior rows —
the model is trusted only for "a revision happened", never for the target.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine
from app.persistence import store as store_mod

REVISION_PHRASINGS = [
    "this substitutes the decision from before",
    "this substitute the decision of before",   # ASR, exactly as spoken
    "we're switching to construction",
    "scrap that, we go with construction",
    "revising the earlier decision",
    "we changed our mind about the niche",
    "we'll do construction instead",
    "in favour of construction",
    "this reverses the earlier call",
    "this supersedes the earlier call",
    "construction instead of insurance",
    "questa sostituisce la decisione di prima",
    "al posto di insurance andiamo su construction",
    "non più insurance, andiamo su construction",
    "cambio idea: costruzioni",
]

NON_REVISIONS = [
    "we will ship the demo on Friday",
    "the niche is insurance for the YC batch",
    "Duccio owns the application",
]


@pytest.mark.parametrize("phrase", REVISION_PHRASINGS)
def test_natural_revision_wording_is_a_supersede_cue(phrase: str) -> None:
    assert store_mod._DECISION_SUPERSEDE_CUE.search(phrase), phrase


@pytest.mark.parametrize("phrase", NON_REVISIONS)
def test_plain_decisions_are_not_revisions(phrase: str) -> None:
    assert not store_mod._revises_earlier({"decision": phrase})


def test_model_flag_survives_normalized_prose() -> None:
    """The linker's second signal: clean prose carrying no cue at all."""
    clean = {"decision": "Use construction as the primary YC niche",
             "related_project": "YC application", "revises_earlier": True}
    assert store_mod._revises_earlier(clean) is True
    # …but a plain record with neither cue nor flag stays unlinked.
    assert store_mod._revises_earlier(
        {"decision": "Use construction as the primary YC niche",
         "related_project": "YC application"}
    ) is False


def test_builder_keeps_the_revision_flag_not_the_target() -> None:
    """_build_decision_records must carry THAT it revises, never WHICH one."""
    from app import meeting_state

    recs = engine._build_decision_records(
        [{"decision": "Use construction as the primary YC niche",
          "decision_maker": "Duccio", "reason": "changed our mind",
          "related_project": "YC application",
          "supersedes": "Use insurance as the primary YC niche"}],
        [],
        meeting_state.MeetingState(),
    )
    assert len(recs) == 1
    assert recs[0]["revises_earlier"] is True
    assert "supersedes" not in recs[0], "the model must not name the target"


def test_two_meetings_link_and_flip_status(tmp_path, monkeypatch) -> None:
    """End to end over the real writer: the second decision links to the first
    and the first flips to superseded — neither stays 'active'."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store_mod)
    org = "org-supersede"

    store_mod.persist_decision_records(org, "bot-1", [{
        "decision": "Use insurance as the primary YC niche",
        "decision_maker": "Duccio", "reason": "RFS #3",
        "related_project": "YC application",
    }])
    store_mod.persist_decision_records(org, "bot-2", [{
        "decision": "Use construction as the primary YC niche",
        "decision_maker": "Duccio",
        "reason": "this substitutes the decision from before",
        "related_project": "YC application",
        "revises_earlier": True,
    }])

    rows = store_mod.list_decisions(org, related_project="YC application")
    by_text = {r["decision"]: r for r in rows}
    old = by_text["Use insurance as the primary YC niche"]
    new = by_text["Use construction as the primary YC niche"]
    assert old["status"] == "superseded", old
    assert new["status"] == "active", new
    assert new["supersedes"] == old["id"], (new, old)
    # history is kept, never deleted
    assert len(rows) == 2


def test_supersede_needs_the_same_project(tmp_path, monkeypatch) -> None:
    """A revision on a DIFFERENT project must never touch an unrelated one."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store_mod)
    org = "org-scoped"

    store_mod.persist_decision_records(org, "bot-1", [{
        "decision": "Use insurance as the primary YC niche",
        "decision_maker": "Duccio", "reason": "", "related_project": "YC application",
    }])
    store_mod.persist_decision_records(org, "bot-2", [{
        "decision": "Switch the pricing page to annual billing",
        "decision_maker": "Duccio", "reason": "",
        "related_project": "Website", "revises_earlier": True,
    }])
    rows = {r["decision"]: r for r in store_mod.list_decisions(org)}
    assert rows["Use insurance as the primary YC niche"]["status"] == "active"
