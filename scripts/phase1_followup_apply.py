from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "scripts" else Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:140]!r}")
    write(path, text.replace(old, new, 1))


# Dashboard: every core typed action must be reviewed from the canonical stored
# action before the approval POST. Saving fields only persists them, then the
# canonical GET is re-read for the preview.
path = "frontend/dashboard.html"
replace_once(
    path,
    '    return \'<button class="btn-appr" data-approve="\'+esc(a.action_id)+\'">Approve &amp; run</button>\';\n',
    '    return \'<button class="btn-appr" data-approve="\'+esc(a.action_id)+\'">Review &amp; approve</button>\';\n',
)

anchor = '  function approveAction(btn,aidArg){\n'
insert = r'''  function closeCanonicalPreview(){
    var s=document.getElementById("canonical-preview-scrim");
    if(s){ if(s._esc) document.removeEventListener("keydown",s._esc,true); s.remove(); }
  }
  function canonicalPreviewValue(name,v){
    if(Array.isArray(v)) return v.join(", ");
    if(v===true) return "Yes";
    if(v===false) return "No";
    var shown=v==null?"":String(v);
    if(["start","end","due_on"].indexOf(String(name))>=0){
      try{ var d=new Date(shown); if(!isNaN(d.getTime())) return d.toLocaleString(undefined,{weekday:"short",year:"numeric",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}); }catch(e){}
    }
    return shown;
  }
  function openCanonicalPreview(act,btn){
    closeCanonicalPreview();
    var schema=act.params_schema||[], params=act.params||{};
    var rows=schema.map(function(f){
      var v=params[f.name];
      if(v==null||v===""||(Array.isArray(v)&&!v.length)) return "";
      return '<div class="prow"><span class="pk">'+esc(f.label||f.name)+'</span><span class="pv">'+esc(canonicalPreviewValue(f.name,v))+'</span></div>';
    }).join("");
    var route=act.route||"";
    var routeLabel=route==="native"?"Laura — your connected account"
      :route==="pipedream"?"Pipedream — your organisation's connected account"
      :route==="cedric"?"Cedric — Slack agent"
      :route==="manual"?"Tracked only — no automatic execution"
      :route==="browser"?"Guarded browser session":route;
    var title={"calendar.create_event":"Create calendar event","email.send":"Send email",
      "asana.create_task":"Create Asana task","asana.update_task":"Update Asana task",
      "asana.add_comment":"Add Asana comment","slack.post_message":"Post Slack message"}[act.tool]||act.action||"Review action";
    var s=document.createElement("div"); s.className="scrim"; s.id="canonical-preview-scrim";
    s.innerHTML='<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cp-t">'+
      '<h2 id="cp-t">'+esc(title)+'</h2>'+
      '<div class="mh">This is the canonical action saved on the server. Nothing runs until you confirm.</div>'+
      '<div class="pbox">'+(rows||'<div class="prow"><span class="pv">'+esc(act.action||"")+'</span></div>')+
      (routeLabel?'<div class="prow"><span class="pk">Runs through</span><span class="pv">'+esc(routeLabel)+'</span></div>':'')+
      (act.risk?'<div class="prow"><span class="pk">Risk</span><span class="pv">'+esc(act.risk)+'</span></div>':'')+
      '</div><div class="macts"><button class="btn" data-cpcancel>Cancel</button>'+
      (schema.length?'<button class="btn" data-cpedit>Edit details</button>':'')+
      '<button class="btn primary" data-cpconfirm>Confirm &amp; run</button></div></div>';
    document.body.appendChild(s);
    s.querySelector("[data-cpcancel]").addEventListener("click",closeCanonicalPreview);
    var edit=s.querySelector("[data-cpedit]");
    if(edit) edit.addEventListener("click",function(){
      closeCanonicalPreview();
      openParamsForm({actionId:act.action_id,schema:schema,missing:act.missing_params||[],
        params:params,approveAfter:true,onApproved:function(){ reviewAction(btn,act.action_id); }});
    });
    s.querySelector("[data-cpconfirm]").addEventListener("click",function(){
      closeCanonicalPreview(); approveAction(btn,act.action_id);
    });
    var esc2=function(e){ if(e.key==="Escape") closeCanonicalPreview(); };
    s._esc=esc2; document.addEventListener("keydown",esc2,true);
    s.addEventListener("mousedown",function(e){ if(e.target===s) closeCanonicalPreview(); });
  }
  function reviewAction(btn,aidArg){
    var aid=aidArg||(btn&&btn.getAttribute("data-approve")); if(!aid) return;
    var old=btn?btn.innerHTML:""; if(btn){ btn.disabled=true; btn.textContent="Loading preview…"; }
    fetch("/dashboard/actions/"+encodeURIComponent(aid),{headers:headers()})
      .then(function(r){ return r.json().catch(function(){return {};}).then(function(j){ return {ok:r.ok,status:r.status,j:j}; }); })
      .then(function(res){
        if(btn){ btn.disabled=false; btn.innerHTML=old; }
        if(!res.ok||!res.j.action) throw new Error(res.j.error||("HTTP "+res.status));
        var act=res.j.action, schema=act.params_schema||[], missing=act.missing_params||[];
        if(missing.length&&schema.length){
          openParamsForm({actionId:aid,schema:schema,missing:missing,params:act.params||{},
            approveAfter:true,onApproved:function(){ reviewAction(btn,aid); }});
          return;
        }
        // Legacy/untyped rescue still enters the approval door so it can be
        // typed. Every core typed action (the seven current types) has a schema
        // and therefore always passes through the canonical preview above.
        if(!schema.length){ approveAction(btn,aid); return; }
        openCanonicalPreview(act,btn);
      })
      .catch(function(e){ if(btn){ btn.disabled=false; btn.innerHTML=old; } toast("Could not load preview: "+e.message,true); });
  }

'''
text = read(path)
if text.count(anchor) != 1:
    raise RuntimeError("frontend/dashboard.html: approveAction anchor mismatch")
write(path, text.replace(anchor, insert + anchor, 1))

replace_once(
    path,
    '            onApproved:function(){ approveAction(btn,aid); }\n',
    '            onApproved:function(){ reviewAction(btn,aid); }\n',
)
replace_once(
    path,
    '            showPreview(Object.assign({},params,args));\n'
    '            return;\n',
    '            closeParamsForm();\n'
    '            reviewAction(null,opts.actionId);\n'
    '            return;\n',
)
replace_once(
    path,
    '          ctl=\'<button class="btn primary" data-approve="\'+esc(a.action_id)+\'">Approve</button>\'+\n',
    '          ctl=\'<button class="btn primary" data-approve="\'+esc(a.action_id)+\'">Review &amp; approve</button>\'+\n',
)
replace_once(
    path,
    '    $$("#ac-list [data-approve]").forEach(function(b){ b.addEventListener("click",function(){ approveAction(b); }); });\n',
    '    $$("#ac-list [data-approve]").forEach(function(b){ b.addEventListener("click",function(){ reviewAction(b); }); });\n',
)
replace_once(
    path,
    '          onApproved:function(){ approveAction(null,aid); }});\n',
    '          onApproved:function(){ reviewAction(null,aid); }});\n',
)
# Other shared action surfaces use the same button attribute/listener pattern.
text = read(path).replace(
    'addEventListener("click",function(){ approveAction(b); });',
    'addEventListener("click",function(){ reviewAction(b); });',
)
write(path, text)

# Pipedream receipts: explicit structured verification, and Asana comments are
# stories (not tasks) when read back.
path = "backend/app/pipedream_executor.py"
replace_once(
    path,
    '    verified = _verify_written(org, account_id, action_type,\n'
    '                               resp.get("json") or {})\n'
    '    return _settle(action_id, org, True, action_type, ref, "",\n'
    '                   kind=(kind + " · verified") if verified else kind)\n',
    '    verified = _verify_written(org, account_id, action_type,\n'
    '                               resp.get("json") or {})\n'
    '    return _settle(\n'
    '        action_id, org, True, action_type, ref, "",\n'
    '        kind=(kind + " · verified") if verified else kind,\n'
    '        verified=verified,\n'
    '        verification="provider readback" if verified else "provider readback unavailable",\n'
    '    )\n',
)
replace_once(
    path,
    '        if action_type.startswith("asana."):\n'
    '            gid = str((data or {}).get("gid") or "")\n'
    '            if not gid:\n'
    '                return False\n'
    '            check = pipedream_client.proxy_request(\n'
    '                org, account_id, "GET",\n'
    '                f"{_ASANA_API}/tasks/{gid}?opt_fields=gid",\n'
    '            )\n'
    '            return bool(check.get("ok"))\n',
    '        if action_type == "asana.add_comment":\n'
    '            story_gid = str((data or {}).get("gid") or "")\n'
    '            if not story_gid:\n'
    '                return False\n'
    '            check = pipedream_client.proxy_request(\n'
    '                org, account_id, "GET",\n'
    '                f"{_ASANA_API}/stories/{story_gid}?opt_fields=gid",\n'
    '            )\n'
    '            return bool(check.get("ok"))\n'
    '        if action_type in ("asana.create_task", "asana.update_task"):\n'
    '            task_gid = str((data or {}).get("gid") or "")\n'
    '            if not task_gid:\n'
    '                return False\n'
    '            check = pipedream_client.proxy_request(\n'
    '                org, account_id, "GET",\n'
    '                f"{_ASANA_API}/tasks/{task_gid}?opt_fields=gid",\n'
    '            )\n'
    '            return bool(check.get("ok"))\n',
)
replace_once(
    path,
    'def _settle(action_id: str, org: str, ok: bool, action_type: str, ref: str,\n'
    '            error: str, *, kind: str = "") -> dict:\n',
    'def _settle(action_id: str, org: str, ok: bool, action_type: str, ref: str,\n'
    '            error: str, *, kind: str = "", verified: bool = False,\n'
    '            verification: str = "") -> dict:\n',
)
replace_once(
    path,
    '    result: dict[str, Any] = {"ok": ok, "kind": kind, "ref": ref}\n',
    '    result: dict[str, Any] = {\n'
    '        "ok": ok, "kind": kind, "ref": ref,\n'
    '        "verified": bool(verified),\n'
    '        "verification": str(verification or ""),\n'
    '    }\n',
)
replace_once(
    path,
    '                receipt={"kind": kind, "ref": ref, "route": "pipedream",\n'
    '                         "runtime": "pipedream"},\n',
    '                receipt={\n'
    '                    "kind": kind, "ref": ref, "route": "pipedream",\n'
    '                    "runtime": "pipedream", "verified": bool(verified),\n'
    '                    "verification": str(verification or ""),\n'
    '                },\n',
)

# Existing test now asserts structured verification; add the comment/story case.
path = "backend/tests/test_pipedream_executor.py"
replace_once(
    path,
    '    assert seen["status"] == "done" and seen["receipt"]["route"] == "pipedream"\n\n\n'
    'def test_execute_approved_no_account_fails(monkeypatch):\n',
    '    assert seen["status"] == "done" and seen["receipt"]["route"] == "pipedream"\n'
    '    assert out["verified"] is True\n'
    '    assert seen["receipt"]["verified"] is True\n\n\n'
    'def test_asana_comment_verifies_the_story_not_a_task(monkeypatch):\n'
    '    _enable_pd(monkeypatch)\n'
    '    monkeypatch.setattr(pipedream_client, "list_accounts",\n'
    '                        lambda org, app="": [{"id": "apn_9", "app": "asana", "healthy": True}])\n'
    '    calls: list[tuple[str, str]] = []\n\n'
    '    def fake_proxy(org, acct, method, url, json_body=None, headers=None):\n'
    '        calls.append((method, url))\n'
    '        return {"ok": True, "status": 200, "json": {"data": {"gid": "story55"}}}\n\n'
    '    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)\n'
    '    seen = _cap_ledger(monkeypatch)\n'
    '    out = pipedream_executor.execute_approved(\n'
    '        "orgX", "act-comment",\n'
    '        {"type": "asana.add_comment", "task": {"task": "42", "text": "Ship it"}},\n'
    '    )\n'
    '    assert out["ok"] is True and out["verified"] is True\n'
    '    assert calls[0][0] == "POST"\n'
    '    assert calls[1][0] == "GET" and "/stories/story55" in calls[1][1]\n'
    '    assert "/tasks/story55" not in calls[1][1]\n'
    '    assert seen["receipt"]["verified"] is True\n\n\n'
    'def test_execute_approved_no_account_fails(monkeypatch):\n',
)

# Static regression: the browser can no longer authorize a core typed action
# without first fetching the canonical record.
test_path = ROOT / "backend/tests/test_phase1_canonical_preview.py"
test_path.write_text(
    '''from pathlib import Path\n\n\ndef test_every_dashboard_approval_uses_canonical_review_gate():\n    root = Path(__file__).resolve().parents[2]\n    html = (root / "frontend" / "dashboard.html").read_text(encoding="utf-8")\n    assert "function reviewAction" in html\n    assert "function openCanonicalPreview" in html\n    assert "reviewAction(null,opts.actionId)" in html\n    assert "showPreview(Object.assign({},params,args))" not in html\n    assert 'function(){ approveAction(b); }' not in html\n    assert "This is the canonical action saved on the server" in html\n''',
    encoding="utf-8",
)

# Postconditions.
html = read("frontend/dashboard.html")
assert "function reviewAction" in html
assert "reviewAction(null,opts.actionId)" in html
assert "showPreview(Object.assign({},params,args))" not in html
assert 'function(){ approveAction(b); }' not in html
pd = read("backend/app/pipedream_executor.py")
assert "/stories/{story_gid}" in pd
assert '"verified": bool(verified)' in pd
print("phase1 follow-up patch applied")
