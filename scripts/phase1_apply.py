from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == 'scripts' else Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding='utf-8')


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding='utf-8')


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{path}: expected one match, found {count}: {old[:120]!r}')
    write(path, text.replace(old, new, 1))


# 1) Canonical schema: one human label vocabulary + one preview builder.
path = 'backend/app/actions/action_plane.py'
replace_once(
    path,
    'RISK_BY_TYPE: dict[str, str] = {\n'
    '    "calendar.create_event": "low",\n'
    '    "email.send": "medium",\n'
    '    "asana.create_task": "low",\n'
    '    "asana.update_task": "low",\n'
    '    "asana.add_comment": "low",\n'
    '    "slack.post_message": "medium",\n'
    '}\n',
    'FIELD_LABELS: dict[str, str] = {\n'
    '    "title": "Title", "start": "Start time", "end": "End time",\n'
    '    "attendees": "Attendees", "description": "Description",\n'
    '    "to": "Recipients", "subject": "Subject", "body": "Message",\n'
    '    "name": "Task name", "notes": "Task description",\n'
    '    "project": "Project", "assignee": "Assignee", "due_on": "Due date",\n'
    '    "subtasks": "Subtasks", "dependencies": "Dependencies",\n'
    '    "attachments": "Attachments", "task": "Task ID",\n'
    '    "completed": "Completed", "text": "Comment",\n'
    '}\n\n'
    'ACTION_LABELS: dict[str, str] = {\n'
    '    "calendar.create_event": "Create calendar event",\n'
    '    "email.send": "Send email",\n'
    '    "asana.create_task": "Create Asana task",\n'
    '    "asana.update_task": "Update Asana task",\n'
    '    "asana.add_comment": "Add Asana comment",\n'
    '    "slack.post_message": "Post Slack message",\n'
    '}\n\n'
    'RISK_BY_TYPE: dict[str, str] = {\n'
    '    "calendar.create_event": "low",\n'
    '    "email.send": "medium",\n'
    '    "asana.create_task": "low",\n'
    '    "asana.update_task": "low",\n'
    '    "asana.add_comment": "low",\n'
    '    "slack.post_message": "medium",\n'
    '}\n'
)
replace_once(
    path,
    '    return [\n'
    '        dict(field)\n'
    '        for field in PARAMS_SCHEMAS.get(str(typed.get("type") or ""), [])\n'
    '    ]\n',
    '    return [\n'
    '        {**dict(field), "label": FIELD_LABELS.get(str(field.get("name") or ""),\n'
    '                                                   str(field.get("name") or "").replace("_", " ").title())}\n'
    '        for field in PARAMS_SCHEMAS.get(str(typed.get("type") or ""), [])\n'
    '    ]\n'
)
replace_once(
    path,
    'def risk_for(typed: dict | None) -> str:\n'
    '    """Return the risk class for a typed action."""\n'
    '    if not isinstance(typed, dict):\n'
    '        return ""\n'
    '    return RISK_BY_TYPE.get(str(typed.get("type") or ""), "")\n',
    'def risk_for(typed: dict | None) -> str:\n'
    '    """Return the risk class for a typed action."""\n'
    '    if not isinstance(typed, dict):\n'
    '        return ""\n'
    '    return RISK_BY_TYPE.get(str(typed.get("type") or ""), "")\n\n\n'
    'def preview_for(typed: dict | None, *, route: str = "") -> dict:\n'
    '    """Canonical approval preview shared by every control surface.\n\n'
    '    It is deliberately built from the stored typed spec and the same schema\n'
    '    used by validation; a browser never invents labels or execution fields.\n'
    '    """\n'
    '    if not isinstance(typed, dict) or not typed.get("type"):\n'
    '        return {}\n'
    '    action_type = str(typed.get("type") or "")\n'
    '    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}\n'
    '    fields: list[dict[str, Any]] = []\n'
    '    for field in params_schema(typed):\n'
    '        name = str(field.get("name") or "")\n'
    '        value = args.get(name)\n'
    '        if _empty(value) and not field.get("required"):\n'
    '            continue\n'
    '        fields.append({\n'
    '            "name": name,\n'
    '            "label": str(field.get("label") or name.replace("_", " ").title()),\n'
    '            "value": value,\n'
    '            "required": bool(field.get("required")),\n'
    '        })\n'
    '    return {\n'
    '        "type": action_type,\n'
    '        "title": ACTION_LABELS.get(action_type, action_type.replace(".", " · ")),\n'
    '        "fields": fields,\n'
    '        "risk": risk_for(typed),\n'
    '        "route": str(route or ""),\n'
    '        "missing_params": missing_params(typed),\n'
    '    }\n'
)

# 2) Canonical Action GET returns the canonical preview.
path = 'backend/app/api/org_api.py'
replace_once(
    path,
    '    receipt = (durable or {}).get("receipt_json")\n'
    '    logs = (durable or {}).get("logs_json")\n'
    '    return {\n',
    '    receipt = (durable or {}).get("receipt_json")\n'
    '    logs = (durable or {}).get("logs_json")\n'
    '    route = str(\n'
    '        (durable or {}).get("execution_route")\n'
    '        or action.get("execution_route") or ""\n'
    '    )\n'
    '    preview = action_plane.preview_for(typed, route=route)\n'
    '    return {\n'
)
replace_once(
    path,
    '        "route": str(\n'
    '            (durable or {}).get("execution_route")\n'
    '            or action.get("execution_route") or ""\n'
    '        ),\n'
    '        "params": dict((typed or {}).get("args") or {}),\n',
    '        "route": route,\n'
    '        "params": dict((typed or {}).get("args") or {}),\n'
    '        "preview": preview,\n'
)

# 3) UI: details save never executes; every approval opens a canonical preview.
path = 'frontend/dashboard.html'
replace_once(
    path,
    '    return \'<button class="btn-appr" data-approve="\'+esc(a.action_id)+\'">Approve &amp; run</button>\';\n',
    '    return \'<button class="btn-appr" data-approve="\'+esc(a.action_id)+\'">Review &amp; approve</button>\';\n'
)
needle = '  function approveAction(btn,aidArg){\n'
insert = r'''  function closeActionPreview(){
    var s=document.getElementById("action-preview-scrim");
    if(s){ if(s._esc) document.removeEventListener("keydown",s._esc,true); s.remove(); }
  }
  function previewValue(v){
    if(Array.isArray(v)) return v.join(", ");
    if(v===true) return "Yes";
    if(v===false) return "No";
    return v==null?"":String(v);
  }
  function openActionPreview(opts){
    closeActionPreview();
    var act=opts.action||{}, p=act.preview||{}, fields=p.fields||[];
    var route=p.route||act.route||"";
    var routeLabel=route==="native"?"Laura — your connected account"
      :route==="pipedream"?"Pipedream — your organisation's connected account"
      :route==="cedric"?"Cedric — Slack agent"
      :route==="browser"?"Guarded browser session"
      :route==="manual"?"Tracked only — no automatic execution":route;
    var rows=fields.map(function(f){
      return '<dt>'+esc(f.label||f.name||"")+'</dt><dd>'+esc(previewValue(f.value))+'</dd>';
    }).join("");
    var s=document.createElement("div"); s.className="scrim"; s.id="action-preview-scrim";
    s.innerHTML='<div class="modal" role="dialog" aria-modal="true" aria-labelledby="apv-t">'+
      '<h2 id="apv-t">'+esc(p.title||act.action||"Review action")+'</h2>'+
      '<div class="mh">Review the exact stored action. Nothing runs until you approve here.</div>'+
      '<dl class="ac-kv">'+rows+
      (routeLabel?'<dt>Runs through</dt><dd>'+esc(routeLabel)+'</dd>':'')+
      ((p.risk||act.risk)?'<dt>Risk</dt><dd>'+esc(p.risk||act.risk)+'</dd>':'')+
      '</dl><div class="macts"><button class="btn" data-apvcancel>Cancel</button>'+
      ((act.params_schema||[]).length?'<button class="btn" data-apvedit>Edit details</button>':'')+
      '<button class="btn primary" data-apvapprove>Approve &amp; run</button></div></div>';
    document.body.appendChild(s);
    s.querySelector("[data-apvcancel]").addEventListener("click",closeActionPreview);
    var edit=s.querySelector("[data-apvedit]");
    if(edit) edit.addEventListener("click",function(){
      closeActionPreview();
      openParamsForm({actionId:act.action_id,schema:act.params_schema||[],
        missing:act.missing_params||[],params:act.params||{},approveAfter:true,
        onApproved:function(){ reviewAction(opts.button,act.action_id); }});
    });
    s.querySelector("[data-apvapprove]").addEventListener("click",function(){
      closeActionPreview(); approveAction(opts.button,act.action_id);
    });
    var esc2=function(e){ if(e.key==="Escape") closeActionPreview(); };
    s._esc=esc2; document.addEventListener("keydown",esc2,true);
    s.addEventListener("mousedown",function(e){ if(e.target===s) closeActionPreview(); });
  }
  function reviewAction(btn,aidArg){
    var aid=aidArg||(btn&&btn.getAttribute("data-approve")); if(!aid) return;
    var old=btn?btn.innerHTML:""; if(btn){ btn.disabled=true; btn.textContent="Loading preview…"; }
    fetch("/dashboard/actions/"+encodeURIComponent(aid),{headers:headers()})
      .then(function(r){ return r.json().catch(function(){return {};}).then(function(j){ return {ok:r.ok,status:r.status,j:j}; }); })
      .then(function(res){
        if(btn){ btn.disabled=false; btn.innerHTML=old; }
        if(!res.ok||!res.j.action) throw new Error(res.j.error||("HTTP "+res.status));
        var act=res.j.action;
        if((act.missing_params||[]).length){
          openParamsForm({actionId:aid,schema:act.params_schema||[],missing:act.missing_params||[],
            params:act.params||{},approveAfter:true,
            onApproved:function(){ reviewAction(btn,aid); }});
          return;
        }
        openActionPreview({action:act,button:btn});
      })
      .catch(function(e){ if(btn){ btn.disabled=false; btn.innerHTML=old; } toast("Could not load preview: "+e.message,true); });
  }

'''
text = read(path)
if text.count(needle) != 1:
    raise RuntimeError('frontend/dashboard.html: approveAction anchor mismatch')
write(path, text.replace(needle, insert + needle, 1))
replace_once(
    path,
    '            approveAfter:true,\n'
    '            onApproved:function(){ approveAction(btn,aid); }\n',
    '            approveAfter:true,\n'
    '            onApproved:function(){ reviewAction(btn,aid); }\n'
)
replace_once(
    path,
    '      \'<div class="mh">This action needs a few details before it can run. Nothing happens until you save.</div>\'+\n',
    '      \'<div class="mh">This action needs a few details. Saving only updates the draft; nothing runs until the separate review and approval step.</div>\'+\n'
)
replace_once(
    path,
    '      (opts.approveAfter?\'<button class="btn primary" data-msaveappr>Save and approve</button>\':\'\')+\n',
    '      (opts.approveAfter?\'<button class="btn primary" data-msaveappr>Save and review</button>\':\'\')+\n'
)
replace_once(
    path,
    '          // inline form runs the action once saved.\n',
    '          // inline form saves only; the next screen is the canonical preview.\n'
)
replace_once(
    path,
    '          ctl=\'<button class="btn primary" data-approve="\'+esc(a.action_id)+\'">Approve</button>\'+\n',
    '          ctl=\'<button class="btn primary" data-approve="\'+esc(a.action_id)+\'">Review &amp; approve</button>\'+\n'
)
replace_once(
    path,
    '    $$("#ac-list [data-approve]").forEach(function(b){ b.addEventListener("click",function(){ approveAction(b); }); });\n',
    '    $$("#ac-list [data-approve]").forEach(function(b){ b.addEventListener("click",function(){ reviewAction(b); }); });\n'
)
replace_once(
    path,
    '          params:act.params||{},approveAfter:true,\n'
    '          onApproved:function(){ approveAction(null,aid); }});\n',
    '          params:act.params||{},approveAfter:true,\n'
    '          onApproved:function(){ reviewAction(null,aid); }});\n'
)
# Any remaining shared surface listeners should also enter through review.
text = read(path)
text = text.replace('addEventListener("click",function(){ approveAction(b); });',
                    'addEventListener("click",function(){ reviewAction(b); });')
write(path, text)

# 4) Truthful verification metadata: provider response is evidence, and core
# Pipedream writes perform a provider GET readback before settling the receipt.
path = 'backend/app/pipedream_executor.py'
replace_once(
    path,
    'ReceiptFn = Callable[[str, dict], tuple]\n',
    'ReceiptFn = Callable[[str, dict], tuple]\n\n\n'
    'def _readback(org: str, account_id: str, action_type: str, args: dict, data: dict) -> tuple[bool, str]:\n'
    '    """Re-read the object just written. A successful write without a readable\n'
    '    provider object remains successful but is explicitly unverified."""\n'
    '    try:\n'
    '        url = ""\n'
    '        expected = ""\n'
    '        if action_type in ("asana.create_task", "asana.update_task"):\n'
    '            expected = str((data.get("data") or {}).get("gid") or args.get("task") or args.get("task_gid") or "")\n'
    '            if expected: url = f"{_ASANA_API}/tasks/{expected}?opt_fields=gid"\n'
    '        elif action_type == "asana.add_comment":\n'
    '            expected = str((data.get("data") or {}).get("gid") or "")\n'
    '            if expected: url = f"{_ASANA_API}/stories/{expected}?opt_fields=gid"\n'
    '        elif action_type == "email.send":\n'
    '            expected = str(data.get("id") or "")\n'
    '            if expected: url = f"{_GMAIL_API}/messages/{expected}?format=metadata"\n'
    '        elif action_type == "gmail.create_draft":\n'
    '            expected = str(data.get("id") or "")\n'
    '            if expected: url = f"{_GMAIL_API}/drafts/{expected}?format=metadata"\n'
    '        elif action_type in ("calendar.create_event", "calendar.update_event"):\n'
    '            expected = str(data.get("id") or args.get("event_id") or args.get("event") or "")\n'
    '            if expected: url = f"{_CAL_API}/calendars/primary/events/{expected}"\n'
    '        if not url:\n'
    '            return False, "provider returned no object id"\n'
    '        check = pipedream_client.proxy_request(org, account_id, "GET", url)\n'
    '        if not check.get("ok"):\n'
    '            return False, f"provider readback HTTP {check.get(\'status\')}"\n'
    '        got = check.get("json") or {}\n'
    '        got_id = str((got.get("data") or {}).get("gid") or got.get("id") or "")\n'
    '        if action_type == "gmail.create_draft" and not got_id:\n'
    '            got_id = str((got.get("message") or {}).get("id") or "")\n'
    '        return bool(got_id and (not expected or got_id == expected)), "provider readback"\n'
    '    except Exception as exc:  # noqa: BLE001 — verification never hides write result\n'
    '        return False, f"verification error ({type(exc).__name__})"\n'
)
replace_once(
    path,
    '    kind, ref = receipt_fn(action_type, resp.get("json") or {})\n'
    '    return _settle(action_id, org, True, action_type, ref, "", kind=kind)\n',
    '    response_json = resp.get("json") or {}\n'
    '    kind, ref = receipt_fn(action_type, response_json)\n'
    '    verified, verification = _readback(org, account_id, action_type, args, response_json)\n'
    '    return _settle(action_id, org, True, action_type, ref, "", kind=kind,\n'
    '                   verified=verified, verification=verification)\n'
)
replace_once(
    path,
    'def _settle(action_id: str, org: str, ok: bool, action_type: str, ref: str,\n'
    '            error: str, *, kind: str = "") -> dict:\n',
    'def _settle(action_id: str, org: str, ok: bool, action_type: str, ref: str,\n'
    '            error: str, *, kind: str = "", verified: bool = False,\n'
    '            verification: str = "") -> dict:\n'
)
replace_once(
    path,
    '    result: dict[str, Any] = {"ok": ok, "kind": kind, "ref": ref}\n',
    '    result: dict[str, Any] = {"ok": ok, "kind": kind, "ref": ref,\n'
    '                              "verified": bool(verified),\n'
    '                              "verification": str(verification or "")}\n'
)
replace_once(
    path,
    '                receipt={"kind": kind, "ref": ref, "route": "pipedream",\n'
    '                         "runtime": "pipedream"},\n',
    '                receipt={"kind": kind, "ref": ref, "route": "pipedream",\n'
    '                         "runtime": "pipedream", "verified": bool(verified),\n'
    '                         "verification": str(verification or "")},\n'
)

# Native adapters expose provider-response verification truthfully. This is not
# called a readback; the receipt says which verification method was used.
path = 'backend/app/runtime/native_runtime.py'
replace_once(
    path,
    '    normalized.setdefault("ref", "")\n'
    '    if not normalized["ok"]:\n',
    '    normalized.setdefault("ref", "")\n'
    '    normalized.setdefault("verified", bool(normalized["ok"] and normalized.get("ref")))\n'
    '    normalized.setdefault("verification",\n'
    '                          "provider write response" if normalized.get("verified") else "")\n'
    '    if not normalized["ok"]:\n'
)

path = 'backend/app/actions/executor.py'
replace_once(
    path,
    '                        "runtime": "laura",\n'
    '                    },\n',
    '                        "runtime": "laura",\n'
    '                        "verified": bool(result.get("verified")),\n'
    '                        "verification": str(result.get("verification") or ""),\n'
    '                    },\n'
)

# 5) Regression tests for the canonical preview and verification receipt fields.
test_path = ROOT / 'backend/tests/test_phase1_action_provenance.py'
test_path.write_text('''from app import action_plane\n\n\ndef test_canonical_preview_uses_schema_labels_and_values():\n    typed = {\n        "type": "calendar.create_event",\n        "args": {\n            "title": "Action review",\n            "start": "2026-07-24T15:00:00+02:00",\n            "end": "2026-07-24T15:30:00+02:00",\n            "attendees": ["anant@sffstudio.com"],\n        },\n    }\n    preview = action_plane.preview_for(typed, route="pipedream")\n    assert preview["title"] == "Create calendar event"\n    assert preview["route"] == "pipedream"\n    assert preview["missing_params"] == []\n    assert {f["label"] for f in preview["fields"]} >= {\n        "Title", "Start time", "End time", "Attendees"\n    }\n\n\ndef test_schema_labels_are_canonical():\n    schema = action_plane.params_schema({"type": "email.send", "args": {}})\n    assert [f["label"] for f in schema] == ["Recipients", "Subject", "Message"]\n''', encoding='utf-8')

# Idempotency / postconditions for the one-shot workflow.
html = read('frontend/dashboard.html')
assert 'Save and approve' not in html
assert 'Save and review' in html
assert 'function reviewAction' in html
assert 'function openActionPreview' in html
assert '"preview": preview' in read('backend/app/api/org_api.py')
assert 'def preview_for' in read('backend/app/actions/action_plane.py')
assert 'provider readback' in read('backend/app/pipedream_executor.py')
print('phase1 patch applied')
