"""Customer avatar surface: only Laura and Cedric are publicly callable."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module
from app import avatars


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.mark.parametrize("avatar_id", ["duccio", "sff"])
def test_guessed_non_customer_avatar_cannot_reach_any_public_surface(
    client, monkeypatch, avatar_id
):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("blocked avatar reached product/vendor work")

    monkeypatch.setattr(main_module, "answer_question", must_not_run)
    monkeypatch.setattr(main_module, "answer_question_stream", must_not_run)
    monkeypatch.setattr(main_module, "answer_with_tools", must_not_run)
    monkeypatch.setattr(main_module, "post_meeting", must_not_run)
    monkeypatch.setattr(main_module.anam_client, "create_persona", must_not_run)
    monkeypatch.setattr(main_module.recall_client, "assert_ready", must_not_run)

    responses = [
        client.post("/demo/ask", json={"avatar_id": avatar_id, "question": "hello"}),
        client.post("/live/ask", json={"avatar_id": avatar_id, "question": "hello"}),
        client.post("/live/act", json={"avatar_id": avatar_id, "question": "hello"}),
        client.post(
            "/demo/post_meeting",
            json={"avatar_id": avatar_id, "transcript": "A: hello"},
        ),
        client.get("/demo/sample", params={"avatar_id": avatar_id}),
        client.post("/live/token", json={"avatar_id": avatar_id}),
        client.post(
            "/sessions/start",
            json={
                "avatar_id": avatar_id,
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        ),
        client.get("/laura-reference.jpg", params={"avatar_id": avatar_id}),
        client.get(f"/{avatar_id}.glb"),
    ]

    assert {response.status_code for response in responses} == {404}
    assert responses[0].json() == {
        "error": "unknown avatar_id",
        "available": ["cedric", "laura"],
    }


@pytest.mark.parametrize("avatar_id", ["laura", "cedric"])
def test_keyfree_demo_still_works_for_customer_avatars(client, monkeypatch, avatar_id):
    monkeypatch.setattr(
        main_module,
        "answer_question",
        lambda avatar, question: {
            "avatar_id": avatar.id,
            "answer": question,
            "citations": [],
        },
    )

    answer = client.post(
        "/demo/ask", json={"avatar_id": avatar_id, "question": "hello"}
    )
    assert answer.status_code == 200
    assert answer.json()["avatar_id"] == avatar_id

    sample = client.get("/demo/sample", params={"avatar_id": avatar_id})
    assert sample.status_code == 200
    assert sample.json()["avatar_id"] == avatar_id


def test_public_roster_is_exactly_customer_enabled():
    assert avatars.CUSTOMER_AVATAR_IDS == {"laura", "cedric"}
    assert avatars.is_customer_enabled("laura")
    assert avatars.is_customer_enabled("cedric")
    assert not avatars.is_customer_enabled("duccio")
    assert not avatars.is_customer_enabled("sff")

    roster = TestClient(main_module.app).get("/avatars").json()["avatars"]
    assert {row["id"] for row in roster} == {"laura", "cedric"}
