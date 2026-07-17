"""Product boundary: seed/reset, stable URLs+ids, and the guarded operation's
no-write / idempotency / visible-confirmation guarantees."""
from __future__ import annotations

from demos.northstar.product import seed, store

PAGES = [
    ("/", "page-home"),
    ("/customers", "customer-acme-robotics"),
    ("/customers/acme-robotics", "acme-blocker"),
    ("/customers/acme-robotics/onboarding", "chk-data-access"),
    ("/customers/acme-robotics/onboarding/integration", "stage-blocker"),
    ("/customers/acme-robotics/implementation", "go-live-readiness"),
    ("/tasks", "tasks-table"),
    ("/security", "sec-two-person"),
]


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["demo"] == "northstar"
    assert j["version"] == seed.DEMO_VERSION
    assert j["tasks"] == 2  # two seed tasks, follow-up absent


def test_all_pages_200_with_stable_testids(client):
    for url, testid in PAGES:
        r = client.get(url)
        assert r.status_code == 200, url
        assert f'data-testid="{testid}"' in r.text, f"{testid} missing on {url}"


def test_stage_urls_are_predictable(client):
    for slug in ("kickoff", "integration", "config", "uat", "golive"):
        r = client.get(f"/customers/acme-robotics/onboarding/{slug}")
        assert r.status_code == 200
        assert 'data-testid="stage-name"' in r.text


def test_followup_absent_initially(client):
    assert store.task_by_idempotency_key(seed.FOLLOWUP_IDEMPOTENCY_KEY) is None
    body = client.get("/tasks").text
    assert 'data-testid="task-task-0003"' not in body
    assert len(store.tasks()) == 2


def test_preview_is_pure_read(client):
    before = list(store.tasks())
    r = client.post("/api/acme/tasks/preview", json={})
    assert r.status_code == 200
    j = r.json()
    assert j["would_create"] is True
    assert j["task"]["id"] is None                     # not written yet
    assert j["task"]["title"] == seed.FOLLOWUP_TITLE
    assert j["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY
    assert store.tasks() == before                     # NO write on preview


def test_rejection_creates_zero_tasks(client):
    # "Rejection" at the product boundary == preview, then never execute.
    client.post("/api/acme/tasks/preview", json={})
    assert store.task_by_idempotency_key(seed.FOLLOWUP_IDEMPOTENCY_KEY) is None
    assert len(store.tasks()) == 2


def test_idempotent_create(client):
    r1 = client.post("/api/acme/tasks", json={})
    assert r1.status_code == 201 and r1.json()["created"] is True
    tid = r1.json()["task"]["id"]
    assert tid == "task-0003"
    assert r1.json()["receipt"] == {
        "status": "created", "task_id": "task-0003",
        "idempotency_key": seed.FOLLOWUP_IDEMPOTENCY_KEY,
        "created_at": seed.DEMO_NOW,
    }
    r2 = client.post("/api/acme/tasks", json={})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["task"]["id"] == tid              # same task
    assert r2.json()["receipt"]["status"] == "exists"
    # exactly one follow-up task, no duplicate
    matches = [t for t in store.tasks()
               if t["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY]
    assert len(matches) == 1
    assert len(store.tasks()) == 3


def test_repeated_execution_stays_single(client):
    for _ in range(5):
        client.post("/api/acme/tasks", json={})
    matches = [t for t in store.tasks()
               if t["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY]
    assert len(matches) == 1


def test_visible_confirmation_after_create(client):
    client.post("/api/acme/tasks", json={})
    body = client.get("/tasks", params={"created": 1}).text
    assert 'data-testid="task-confirmation"' in body
    assert 'data-testid="task-task-0003"' in body
    assert seed.FOLLOWUP_TITLE in body


def test_reset_restores_state(client):
    client.post("/api/acme/tasks", json={})
    assert len(store.tasks()) == 3
    r = client.post("/admin/reset")
    assert r.status_code == 200 and r.json()["tasks"] == 2
    assert store.task_by_idempotency_key(seed.FOLLOWUP_IDEMPOTENCY_KEY) is None
    # reset is idempotent
    client.post("/admin/reset")
    assert len(store.tasks()) == 2
