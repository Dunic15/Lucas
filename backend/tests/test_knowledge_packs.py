"""Knowledge packs remain usable without exposing them as callable avatars."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402


def test_laura_no_longer_bundles_sff_pack():
    # SFF was moved to web search — laura's index must NOT pull the sff docs in.
    laura = avatars.load("laura")
    assert "sff" not in (laura.knowledge_packs or [])
    assert laura.knowledge_dirs == [laura.knowledge_dir]


def test_missing_pack_is_ignored_not_fatal():
    laura = avatars.load("laura")
    laura.knowledge_packs = ["does-not-exist"]
    assert laura.knowledge_dirs == [laura.knowledge_dir]


def test_sff_is_a_knowledge_pack_not_a_callable_avatar():
    pack = avatars.settings.avatars_dir / "sff"
    assert (pack / "knowledge").is_dir()
    assert not (pack / "avatar.yaml").exists()


def test_sff_sectors_are_owner_fill_not_invented():
    """Hard rule: the site publishes no sectors — every Sector line must be
    owner-fill, and portfolio facts must carry their source."""
    doc = (
        avatars.settings.avatars_dir / "sff" / "knowledge" / "portfolio_companies.md"
    ).read_text()
    sector_lines = [l for l in doc.splitlines() if l.startswith("- Sector:")]
    assert sector_lines, "portfolio doc lost its Sector fields"
    assert all("to be filled by owner" in l for l in sector_lines)
    assert doc.count("- Source: https://sff.vc") >= 40  # every entry sourced
