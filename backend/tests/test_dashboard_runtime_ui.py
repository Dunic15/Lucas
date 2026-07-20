"""Action Center defaults to recent meetings without hiding history."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dashboard_runtime_ui import enhance_dashboard_html  # noqa: E402


def test_injects_latest_eight_and_browse_older_control():
    source = """<html><script>
function actionIndex(){ return []; }
function renderActions(){ return true; }
})();
</script></html>"""
    out = enhance_dashboard_html(source)

    assert out != source
    assert ".slice(0,8)" in out
    assert "Browse older meetings" in out
    assert "Show latest 8" in out
    assert "_acOriginalActionIndex" in out


def test_does_not_modify_unrelated_html():
    source = "<html><body>not the Laura dashboard</body></html>"
    assert enhance_dashboard_html(source) == source


def test_injection_is_presentation_only():
    source = """<script>
function actionIndex(){ return []; }
function renderActions(){ return true; }
})();
</script>"""
    out = enhance_dashboard_html(source)

    assert "DATA.meetings" in out
    assert "DELETE" not in out
    assert "fetch(" not in out
