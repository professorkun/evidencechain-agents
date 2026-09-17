from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app.dashboard import create_app
from app.taskboard import StateError, TaskBoard


def test_task_requires_approval_before_queue(tmp_path) -> None:
    board = TaskBoard(tmp_path / "taskboard.sqlite3")
    task = board.create_task("安全演练", {"allowed_paths": ["docs"]})
    board.transition(task["id"], "deliberating")
    board.transition(task["id"], "awaiting_approval")

    approved = board.approve(task["id"], "approve_execution")

    assert approved["status"] == "queued"
    assert [event["type"] for event in approved["events"]] == ["created", "transition", "transition", "transition"]


def test_taskboard_rejects_invalid_transition(tmp_path) -> None:
    board = TaskBoard(tmp_path / "taskboard.sqlite3")
    task = board.create_task("不能跳过确认")
    with pytest.raises(StateError, match="cannot transition"):
        board.transition(task["id"], "running")


def test_dashboard_serves_local_board_and_approval_api(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "taskboard.sqlite3"))
    assert client.get("/").status_code == 200
    created = client.post("/api/tasks", json={"title": "API 演练"}).json()
    task_id = created["id"]
    client.post(f"/api/tasks/{task_id}/transition/deliberating")
    client.post(f"/api/tasks/{task_id}/transition/awaiting_approval")

    approved = client.post(f"/api/tasks/{task_id}/approve", json={"action": "approve_execution"})

    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"


def test_dashboard_runs_three_discussion_roles_before_approval(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "taskboard.sqlite3"))
    created = client.post("/api/tasks", json={"title": "多角色讨论演练"}).json()
    task_id = created["id"]
    client.post(f"/api/tasks/{task_id}/transition/deliberating")

    discussed = client.post(f"/api/tasks/{task_id}/discuss")

    assert discussed.status_code == 200
    reports = discussed.json()["agent_reports"]
    assert [report["role"] for report in reports] == ["方案 Agent", "反方 Agent", "调研 Agent"]
    assert all("离线模拟" in report["content"] for report in reports)
    assert client.post(f"/api/tasks/{task_id}/transition/awaiting_approval").status_code == 200


def test_dashboard_runs_readonly_executor_and_verifier_after_approval(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "taskboard.sqlite3"))
    task = client.post("/api/tasks", json={"title": "执行与验证演练"}).json()
    task_id = task["id"]
    client.post(f"/api/tasks/{task_id}/transition/deliberating")
    client.post(f"/api/tasks/{task_id}/discuss")
    client.post(f"/api/tasks/{task_id}/transition/awaiting_approval")
    client.post(f"/api/tasks/{task_id}/approve", json={"action": "approve_readonly"})

    executed = client.post(f"/api/tasks/{task_id}/execute-readonly")
    assert executed.status_code == 200
    assert executed.json()["status"] == "verifying"
    assert executed.json()["agent_runs"][-1]["role"] == "执行 Agent"

    verified = client.post(f"/api/tasks/{task_id}/verify")
    assert verified.status_code == 200
    assert verified.json()["status"] == "awaiting_merge"
    assert verified.json()["agent_runs"][-1]["role"] == "验证 Agent"
