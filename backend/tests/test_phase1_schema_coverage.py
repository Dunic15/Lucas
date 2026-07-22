from app import action_plane, pipedream_executor


def test_every_core_pipedream_mapper_has_a_canonical_schema():
    mapped = pipedream_executor.action_types()
    assert mapped
    assert mapped <= set(action_plane.PARAMS_SCHEMAS)


def test_calendar_update_and_gmail_draft_required_fields():
    assert action_plane.missing_params({"type": "calendar.update_event", "args": {}}) == ["event_id"]
    assert action_plane.missing_params({"type": "gmail.create_draft", "args": {}}) == ["to", "subject", "body"]
