from app import action_plane


def test_canonical_preview_uses_schema_labels_and_values():
    typed = {
        "type": "calendar.create_event",
        "args": {
            "title": "Action review",
            "start": "2026-07-24T15:00:00+02:00",
            "end": "2026-07-24T15:30:00+02:00",
            "attendees": ["anant@sffstudio.com"],
        },
    }
    preview = action_plane.preview_for(typed, route="pipedream")
    assert preview["title"] == "Create calendar event"
    assert preview["route"] == "pipedream"
    assert preview["missing_params"] == []
    assert {f["label"] for f in preview["fields"]} >= {
        "Title", "Start time", "End time", "Attendees"
    }


def test_schema_labels_are_canonical():
    schema = action_plane.params_schema({"type": "email.send", "args": {}})
    assert [f["label"] for f in schema] == ["Recipients", "Subject", "Message"]
