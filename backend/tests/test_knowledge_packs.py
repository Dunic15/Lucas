"""Knowledge packs: laura's index includes the sff pack; no invented facts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402


def test_laura_references_sff_pack():
    laura = avatars.load("laura")
    assert "sff" in (laura.knowledge_packs or [])
    dirs = laura.knowledge_dirs
    assert laura.knowledge_dir in dirs
    assert any(d.name == "knowledge" and d.parent.name == "sff" for d in dirs)


def test_missing_pack_is_ignored_not_fatal():
    laura = avatars.load("laura")
    laura.knowledge_packs = ["does-not-exist"]
    assert laura.knowledge_dirs == [laura.knowledge_dir]


def test_sff_avatar_loads_with_both_wake_words():
    sff = avatars.load("sff")
    assert "laura" in sff.wake_words and "sff" in sff.wake_words


def test_sff_sectors_are_owner_fill_not_invented():
    """Hard rule: the site publishes no sectors — every Sector line must be
    owner-fill, and portfolio facts must carry their source."""
    doc = (avatars.load("sff").knowledge_dir / "portfolio_companies.md").read_text()
    sector_lines = [l for l in doc.splitlines() if l.startswith("- Sector:")]
    assert sector_lines, "portfolio doc lost its Sector fields"
    assert all("to be filled by owner" in l for l in sector_lines)
    assert doc.count("- Source: https://sff.vc") >= 40  # every entry sourced
