from pathlib import Path


def test_every_dashboard_approval_uses_canonical_review_gate():
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "dashboard.html").read_text(encoding="utf-8")
    assert "function reviewAction" in html
    assert "function openCanonicalPreview" in html
    assert "reviewAction(null,opts.actionId)" in html
    assert "showPreview(Object.assign({},params,args))" not in html
    assert 'function(){ approveAction(b); }' not in html
    assert "This is the canonical action saved on the server" in html
