"""The live 'repair' line offers PER-AVATAR topics, loaded from avatar.yaml —
so the SFF fund expert never offers onboarding help and vice-versa. Uses the
real avatar configs on disk; no network / keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, main  # noqa: E402


def test_laura_repair_line_offers_onboarding_topics():
    line = main._silent_answer_repair_line(avatars.load("laura"))
    assert "I can hear you" in line
    assert "onboarding" in line
    assert "access/security" in line
    assert "AI Buffer" in line
    # It must NOT leak the other avatar's domain.
    assert "portfolio" not in line


def test_sff_repair_line_offers_fund_and_portfolio_topics():
    line = main._silent_answer_repair_line(avatars.load("sff"))
    assert "I can hear you" in line
    assert "portfolio" in line
    assert "fund thesis" in line
    assert "mentor/bootcamp" in line
    # It must NOT offer Laura's onboarding domain.
    assert "onboarding" not in line
    assert "access/security" not in line


def test_repair_line_falls_back_when_no_topics_hint():
    avatar = avatars.load("laura")
    object.__setattr__(avatar, "topics_hint", "")
    line = main._silent_answer_repair_line(avatar)
    assert "the process documents I was given" in line
