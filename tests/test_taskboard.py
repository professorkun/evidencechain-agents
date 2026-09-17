from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
import subprocess
from pathlib import Path

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


def test_dashboard_imports_real_codex_reports_without_relabeling_them_as_mock(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "taskboard.sqlite3"))
    task = client.post("/api/tasks", json={"title": "真实协作归档"}).json()
    task_id = task["id"]
    client.post(f"/api/tasks/{task_id}/transition/deliberating")

    imported = client.post(
        f"/api/tasks/{task_id}/import-reports",
        json={"reports": {"调研 Agent": "已读取本机证据；未调用工作台模拟模型。"}},
    )

    assert imported.status_code == 200
    assert imported.json()["agent_reports"] == [
        {
            "role": "调研 Agent",
            "status": "completed",
            "content": "已读取本机证据；未调用工作台模拟模型。",
            "source": "codex-live-import",
            "at": imported.json()["agent_reports"][0]["at"],
        }
    ]


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


def test_dashboard_git_worktree_verify_merge_and_lock_release(tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    (repository / "src").mkdir()
    (repository / "src" / "demo.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "seed"], check=True, capture_output=True)
    contract = {
        "repository": str(repository),
        "base_ref": "main",
        "allowed_paths": ["src"],
        "test_commands": [["{python}", "-c", "print('verified')"]],
        "acceptance_criteria": ["tests pass"],
    }
    client = TestClient(create_app(tmp_path / "taskboard.sqlite3", tmp_path / "worktrees", tmp_path / "locks"))
    task = client.post("/api/tasks", json={"title": "Git 安全演练", "contract": contract}).json()
    task_id = task["id"]
    client.post(f"/api/tasks/{task_id}/transition/deliberating")
    client.post(f"/api/tasks/{task_id}/transition/awaiting_approval")
    client.post(f"/api/tasks/{task_id}/approve", json={"action": "approve_git"})

    prepared = client.post(f"/api/tasks/{task_id}/prepare-git")
    assert prepared.status_code == 200
    worktree = Path(prepared.json()["git_lease"]["worktree"])
    (worktree / "src" / "demo.txt").write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "src/demo.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-m", "change"], check=True, capture_output=True)

    verified = client.post(f"/api/tasks/{task_id}/verify-git")
    assert verified.status_code == 200
    assert verified.json()["status"] == "awaiting_merge"
    merged = client.post(f"/api/tasks/{task_id}/merge", json={"confirm": True})
    assert merged.status_code == 200
    assert merged.json()["status"] == "completed"
    assert merged.json()["git_lease"]["lock_state"] == "released"
    assert (repository / "src" / "demo.txt").read_text(encoding="utf-8") == "after\n"
