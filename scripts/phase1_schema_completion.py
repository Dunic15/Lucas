from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, got {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


# Complete the canonical vocabulary for every hand-mapped Pipedream core type.
path = "backend/app/actions/action_plane.py"
replace_once(
    path,
    '        _FIELD(name="description", type="string", required=False,\n'
    '               label="description", label_it="descrizione"),\n'
    '    ],\n'
    '    "email.send": [\n',
    '        _FIELD(name="description", type="string", required=False,\n'
    '               label="description", label_it="descrizione"),\n'
    '    ],\n'
    '    "calendar.update_event": [\n'
    '        _FIELD(name="event_id", type="string", required=True,\n'
    '               label="event ID", label_it="ID evento"),\n'
    '        _FIELD(name="title", type="string", required=False,\n'
    '               label="title", label_it="titolo"),\n'
    '        _FIELD(name="start", type="string", required=False,\n'
    '               description="ISO 8601 start", label="start time", label_it="orario"),\n'
    '        _FIELD(name="end", type="string", required=False,\n'
    '               description="ISO 8601 end", label="end time", label_it="orario di fine"),\n'
    '        _FIELD(name="description", type="string", required=False,\n'
    '               label="description", label_it="descrizione"),\n'
    '    ],\n'
    '    "email.send": [\n',
)
replace_once(
    path,
    '        _FIELD(name="body", type="string", required=True,\n'
    '               slot="email_body", label="message text", label_it="testo"),\n'
    '    ],\n'
    '    "asana.create_task": [\n',
    '        _FIELD(name="body", type="string", required=True,\n'
    '               slot="email_body", label="message text", label_it="testo"),\n'
    '    ],\n'
    '    "gmail.create_draft": [\n'
    '        _FIELD(name="to", type="array", required=True,\n'
    '               description="recipient emails", slot="email_to",\n'
    '               label="recipient", label_it="destinatario"),\n'
    '        _FIELD(name="subject", type="string", required=True,\n'
    '               label="subject", label_it="oggetto"),\n'
    '        _FIELD(name="body", type="string", required=True,\n'
    '               slot="email_body", label="message text", label_it="testo"),\n'
    '    ],\n'
    '    "asana.create_task": [\n',
)
replace_once(
    path,
    '        _FIELD(name="subtasks", type="array", required=False,\n'
    '               description="subtask titles, one per entry"),\n'
    '        _FIELD(name="dependencies", type="array", required=False,\n'
    '               description="tasks this depends on (name or gid)"),\n'
    '        _FIELD(name="attachments", type="array", required=False,\n'
    '               description="attachment URLs"),\n',
    '        _FIELD(name="subtasks", type="array", required=False,\n'
    '               description="subtask titles, one per entry",\n'
    '               label="subtasks", label_it="sotto-attività"),\n'
    '        _FIELD(name="dependencies", type="array", required=False,\n'
    '               description="tasks this depends on (name or gid)",\n'
    '               label="dependencies", label_it="dipendenze"),\n'
    '        _FIELD(name="attachments", type="array", required=False,\n'
    '               description="attachment URLs",\n'
    '               label="attachments", label_it="allegati"),\n',
)
replace_once(
    path,
    '            required=True,\n'
    '            description="task gid",\n'
    '        ),\n'
    '        _FIELD(name="completed", type="boolean", required=False),\n'
    '        _FIELD(name="due_on", type="string", required=False),\n'
    '    ],\n'
    '    "asana.add_comment": [\n',
    '            required=True,\n'
    '            description="task gid",\n'
    '            label="task ID", label_it="ID attività",\n'
    '        ),\n'
    '        _FIELD(name="completed", type="boolean", required=False,\n'
    '               label="completed", label_it="completata"),\n'
    '        _FIELD(name="due_on", type="string", required=False,\n'
    '               label="due date", label_it="scadenza"),\n'
    '    ],\n'
    '    "asana.add_comment": [\n',
)
replace_once(
    path,
    '            required=True,\n'
    '            description="task gid",\n'
    '        ),\n'
    '        _FIELD(name="text", type="string", required=True),\n'
    '    ],\n'
    '    "slack.post_message": [\n',
    '            required=True,\n'
    '            description="task gid",\n'
    '            label="task ID", label_it="ID attività",\n'
    '        ),\n'
    '        _FIELD(name="text", type="string", required=True,\n'
    '               label="comment text", label_it="testo commento"),\n'
    '    ],\n'
    '    "slack.post_message": [\n',
)
replace_once(
    path,
    '            required=True,\n'
    '            description="message text to post to the connected Slack channel",\n'
    '        ),\n',
    '            required=True,\n'
    '            description="message text to post to the connected Slack channel",\n'
    '            label="message text", label_it="testo messaggio",\n'
    '        ),\n',
)
replace_once(
    path,
    '    "calendar.create_event": "low",\n'
    '    "email.send": "medium",\n',
    '    "calendar.create_event": "low",\n'
    '    "calendar.update_event": "medium",\n'
    '    "email.send": "medium",\n'
    '    "gmail.create_draft": "low",\n',
)

# Any typed action gets a canonical preview. Unknown/generic typed actions use
# their stored parameter keys; nested props are rendered as JSON, never
# "[object Object]". Only truly untyped legacy captures enter the rescue door.
path = "frontend/dashboard.html"
replace_once(
    path,
    '    if(v===false) return "No";\n'
    '    var shown=v==null?"":String(v);\n',
    '    if(v===false) return "No";\n'
    '    if(v&&typeof v==="object"){ try{return JSON.stringify(v,null,2);}catch(e){} }\n'
    '    var shown=v==null?"":String(v);\n',
)
replace_once(
    path,
    '    var schema=act.params_schema||[], params=act.params||{};\n'
    '    var rows=schema.map(function(f){\n',
    '    var schema=act.params_schema||[], params=act.params||{};\n'
    '    var fields=schema.length?schema:Object.keys(params).map(function(k){\n'
    '      return {name:k,label:String(k).replace(/_/g," ")};\n'
    '    });\n'
    '    var rows=fields.map(function(f){\n',
)
replace_once(
    path,
    '    var title={"calendar.create_event":"Create calendar event","email.send":"Send email",\n'
    '      "asana.create_task":"Create Asana task","asana.update_task":"Update Asana task",\n',
    '    var title={"calendar.create_event":"Create calendar event",\n'
    '      "calendar.update_event":"Update calendar event",\n'
    '      "email.send":"Send email","gmail.create_draft":"Create email draft",\n'
    '      "asana.create_task":"Create Asana task","asana.update_task":"Update Asana task",\n',
)
replace_once(
    path,
    '        // Legacy/untyped rescue still enters the approval door so it can be\n'
    '        // typed. Every core typed action (the seven current types) has a schema\n'
    '        // and therefore always passes through the canonical preview above.\n'
    '        if(!schema.length){ approveAction(btn,aid); return; }\n'
    '        openCanonicalPreview(act,btn);\n',
    '        // Legacy/untyped rescue still enters the approval door so it can be\n'
    '        // typed. Every action that already has a stored type — core or a\n'
    '        // generic pd.<app>.run action — must pass through this preview.\n'
    '        if(!act.tool){ approveAction(btn,aid); return; }\n'
    '        openCanonicalPreview(act,btn);\n',
)

# Expand regression tests to cover the entire fixed mapper registry and generic
# typed-action preview fallback.
path = "backend/tests/test_phase1_canonical_preview.py"
content = read(path)
content = content.replace(
    '    assert "This is the canonical action saved on the server" in html\n',
    '    assert "This is the canonical action saved on the server" in html\n'
    '    assert "if(!act.tool){ approveAction(btn,aid); return; }" in html\n'
    '    assert "Object.keys(params).map" in html\n'
    '    assert "JSON.stringify(v,null,2)" in html\n',
)
write(path, content)

schema_test = ROOT / "backend/tests/test_phase1_schema_coverage.py"
schema_test.write_text(
    '''from app import action_plane, pipedream_executor\n\n\ndef test_every_core_pipedream_mapper_has_a_canonical_schema():\n    mapped = pipedream_executor.action_types()\n    assert mapped\n    assert mapped <= set(action_plane.PARAMS_SCHEMAS)\n\n\ndef test_calendar_update_and_gmail_draft_required_fields():\n    assert action_plane.missing_params({"type": "calendar.update_event", "args": {}}) == ["event_id"]\n    assert action_plane.missing_params({"type": "gmail.create_draft", "args": {}}) == ["to", "subject", "body"]\n''',
    encoding="utf-8",
)

assert '"calendar.update_event": [' in read("backend/app/actions/action_plane.py")
assert '"gmail.create_draft": [' in read("backend/app/actions/action_plane.py")
assert "if(!act.tool){ approveAction(btn,aid); return; }" in read("frontend/dashboard.html")
print("Phase 1 schema and generic preview completed")
