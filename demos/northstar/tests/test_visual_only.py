"""Proof that the onboarding-diagram target is VISUAL-ONLY.

The acceptance property: a reader using text or the accessibility tree alone
cannot pick the blocked stage, because all five nodes share one accessible label
and carry no stage-name text. The blocked stage is knowable only from fill
colour + position. These tests parse the served HTML and assert exactly that -
network-free, deterministic. (An optional live browser check that clicks by
colour is described in README; it is not required for this proof.)
"""
from __future__ import annotations

from html.parser import HTMLParser

from demos.northstar.product import seed


class _StageNodes(HTMLParser):
    """Collect <a class="stage-node"> elements: their attrs and inner text."""
    def __init__(self):
        super().__init__()
        self.nodes: list[dict] = []
        self._depth = 0
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "stage-node" in (a.get("class") or ""):
            self._cur = {"attrs": a, "text": ""}
            self._depth = 1
            return
        if self._cur is not None:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._cur is not None:
            self._depth -= 1
            if self._depth == 0:
                self.nodes.append(self._cur)
                self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data


def _nodes(client) -> list[dict]:
    html = client.get("/customers/acme-robotics").text
    p = _StageNodes()
    p.feed(html)
    return p.nodes


def test_diagram_has_exactly_five_stage_nodes(client):
    assert len(_nodes(client)) == 5


def test_all_nodes_share_one_identical_accessible_label(client):
    labels = {n["attrs"].get("aria-label") for n in _nodes(client)}
    assert labels == {"Onboarding stage"}          # one label, for all five


def test_nodes_carry_no_stage_name_text(client):
    # No node exposes its stage name as text; so text scraping is useless.
    stage_names = {s["name"].lower() for s in seed.ONBOARDING_STAGES}
    for n in _nodes(client):
        txt = n["text"].strip().lower()
        assert txt == "", f"node leaked text {txt!r}"
        for name in stage_names:
            assert name not in txt


def test_text_or_ax_selection_is_ambiguous(client):
    # Selecting "the Onboarding stage" by accessible name matches all five and
    # gives no way to know which href is Data Integration -> ambiguous by design.
    nodes = _nodes(client)
    by_label = [n for n in nodes if n["attrs"].get("aria-label") == "Onboarding stage"]
    assert len(by_label) == 5
    hrefs = {n["attrs"]["href"] for n in by_label}
    assert len(hrefs) == 5                           # five different destinations
    # ...yet the accessible label cannot map name -> the integration href.


def test_target_is_identifiable_only_by_visual_signal(client):
    # The blocked stage is the one whose ONLY distinguishing signals are the
    # amber fill and second position; both visual.
    nodes = _nodes(client)
    target = seed.ONBOARDING_STAGES[1]               # stage-integration
    assert target["id"] == seed.VISUAL_TARGET_STAGE_ID
    amber = [n for n in nodes if n["attrs"].get("data-fill") == target["visual"]["fill"]]
    assert len(amber) == 1                           # unique by colour
    node = amber[0]
    assert node["attrs"]["id"] == "stage-integration"
    assert node["attrs"]["data-order"] == "1"        # unique by position
    assert node["attrs"]["href"] == "/customers/acme-robotics/onboarding/integration"
    assert node["attrs"]["data-status"] == "blocked"


def test_fixture_stays_operable(client):
    # "Accessible as a demo fixture": nodes are real, focusable links (operable),
    # even though their labels are deliberately non-distinguishing.
    for n in _nodes(client):
        assert n["attrs"].get("role") == "link"
        assert n["attrs"].get("tabindex") == "0"
        assert n["attrs"].get("href", "").startswith(
            "/customers/acme-robotics/onboarding/")
