"""Local-only FastAPI dashboard. It records approvals but does not execute tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.taskboard import StateError, TaskBoard
from app.main import execute_readonly_node, run_discussion
from app.git_workflow import (
    contract_from_task,
    executor_brief,
    merge_verified_task,
    prepare_task_worktree,
    release_task_lock,
    serialize_lease,
    verified_worktree_commit,
    verify_task_worktree,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "taskboard.sqlite3"
WORKTREES_ROOT = ROOT / "data" / "worktrees"
LOCKS_ROOT = ROOT / "data" / "locks"
STATIC_PAGE = ROOT / "app" / "static" / "dashboard.html"


class CreateTaskRequest(BaseModel):
    title: str
    contract: dict[str, Any] = {}
    source: str = "workbench"
    external_task_id: str | None = None


class ApprovalRequest(BaseModel):
    action: str = "approve_execution"
    approver: str = "user"


class ImportReportsRequest(BaseModel):
    """Explicitly imported reports from a real Codex collaboration turn."""

    reports: dict[str, str]
    source: str = "codex-live-import"


class MergeRequest(BaseModel):
    confirm: bool = False


class ExternalEventRequest(BaseModel):
    external_task_id: str
    title: str = ""
    event_id: str
    event_type: str
    status: str | None = None
    message: str = ""
    output: str = ""


def create_app(
    database_path: Path = DEFAULT_DATABASE,
    worktrees_root: Path = WORKTREES_ROOT,
    locks_root: Path = LOCKS_ROOT,
) -> FastAPI:
    board = TaskBoard(database_path)
    app = FastAPI(title="Multi-Agent Workbench", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_PAGE)

    @app.get("/api/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return board.list_tasks()

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        try:
            return board.get_task(task_id)
        except StateError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/tasks")
    def create_task(request: CreateTaskRequest) -> dict[str, Any]:
        try:
            return board.create_task(
                request.title,
                request.contract,
                source=request.source,
                external_task_id=request.external_task_id,
                sync_state="live" if request.external_task_id else "local",
                control_mode="observe_only" if request.source == "codex-bridge" else "workbench",
            )
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/events")
    def ingest_external_event(request: ExternalEventRequest) -> dict[str, Any]:
        try:
            return board.ingest_external_event(**request.model_dump())
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, bool]:
        try:
            board.delete_task(task_id)
            return {"deleted": True}
        except StateError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/transition/{status}")
    def transition(task_id: str, status: str) -> dict[str, Any]:
        try:
            return board.transition(task_id, status)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: str, request: ApprovalRequest) -> dict[str, Any]:
        try:
            return board.approve(task_id, request.action, approver=request.approver)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str) -> dict[str, Any]:
        try:
            return board.cancel(task_id)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str) -> dict[str, Any]:
        try:
            return board.retry(task_id)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/discuss")
    async def discuss(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            reports = await run_discussion(task["title"])
            return board.save_agent_reports(task_id, reports)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"discussion agents unavailable: {error}") from error

    @app.post("/api/tasks/{task_id}/import-reports")
    def import_reports(task_id: str, request: ImportReportsRequest) -> dict[str, Any]:
        """Record actual external role outputs; never relabel them as model calls from this dashboard."""
        if not request.reports or any(not role.strip() or not content.strip() for role, content in request.reports.items()):
            raise HTTPException(status_code=400, detail="reports must contain non-empty role names and content")
        try:
            return board.save_agent_reports(task_id, request.reports, source=request.source)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/execute-readonly")
    async def execute_readonly(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            if task["status"] != "queued":
                raise StateError("only approved queued tasks can start readonly execution")
            board.transition(task_id, "running", actor="agent:执行 Agent")
            result = await execute_readonly_node({"task": task["title"]})
            content = "\n\n".join(f"### {name}\n{value}" for name, value in result["execution"].items())
            passed = all("exit=0" in value for value in result["execution"].values() if value.startswith("exit="))
            board.save_agent_run(task_id, "执行 Agent", "execution", "completed" if passed else "failed", content)
            if not passed:
                return board.transition(task_id, "failed", actor="agent:执行 Agent")
            return board.transition(task_id, "verifying", actor="agent:执行 Agent")
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:
            try:
                board.save_agent_run(task_id, "执行 Agent", "execution", "failed", str(error))
                board.transition(task_id, "failed", actor="agent:执行 Agent")
            except StateError:
                pass
            raise HTTPException(status_code=500, detail=f"readonly execution failed: {error}") from error

    @app.post("/api/tasks/{task_id}/verify")
    def verify(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            if task["status"] != "verifying":
                raise StateError("only tasks with readonly execution evidence can be verified")
            execution = [run for run in task["agent_runs"] if run["phase"] == "execution"]
            passed = bool(execution) and execution[-1]["status"] == "completed" and "### inspect" in execution[-1]["content"] and "### test" in execution[-1]["content"]
            report = "验证通过：已找到执行 Agent 的文件清单和测试结果，未发现写入步骤。" if passed else "验证失败：缺少完整的只读执行证据。"
            board.save_agent_run(task_id, "验证 Agent", "verification", "completed" if passed else "failed", report)
            return board.transition(task_id, "awaiting_merge" if passed else "failed", actor="agent:验证 Agent")
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/prepare-git")
    def prepare_git(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            if task["status"] != "queued":
                raise StateError("only an approved queued task can create a Git worktree")
            if task["git_lease"]:
                raise StateError("task already has a Git worktree lease")
            lease, contract = prepare_task_worktree(task, worktrees_root, locks_root)
            repository, _, _ = contract_from_task(task)
            record = serialize_lease(lease, contract, repository)
            board.save_git_lease(task_id, record)
            board.save_agent_run(task_id, "执行 Agent", "git_execution", "ready", executor_brief(task, record))
            return board.transition(task_id, "running", actor="agent:执行 Agent")
        except (StateError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/verify-git")
    def verify_git(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            if task["status"] != "running" or not task["git_lease"]:
                raise StateError("only a running Git task with an active worktree can be verified")
            board.transition(task_id, "verifying", actor="agent:验证 Agent")
            try:
                result, plan = verify_task_worktree(task_id, task["git_lease"])
                verified_commit = verified_worktree_commit(task_id, task["git_lease"])
            except ValueError as error:
                board.save_agent_run(task_id, "验证 Agent", "git_verification", "failed", str(error))
                return board.transition(task_id, "failed", actor="agent:验证 Agent")
            passed = result.passed and bool(result.changed_files)
            content = plan + "\n\n测试结果：\n" + "\n\n".join(result.command_results)
            board.save_agent_run(task_id, "验证 Agent", "git_verification", "completed" if passed else "failed", content)
            if not passed:
                return board.transition(task_id, "failed", actor="agent:验证 Agent")
            board.update_git_lease(task_id, verified_commit=verified_commit)
            return board.transition(task_id, "awaiting_merge", actor="agent:验证 Agent")
        except (StateError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/merge")
    def merge(task_id: str, request: MergeRequest) -> dict[str, Any]:
        try:
            if not request.confirm:
                raise StateError("merge requires explicit confirmation")
            task = board.get_task(task_id)
            if task["status"] != "awaiting_merge" or not task["git_lease"]:
                raise StateError("only verified Git tasks can be merged")
            commit = merge_verified_task(task_id, task["git_lease"])
            release_task_lock(task_id, task["git_lease"], locks_root)
            board.update_git_lease(task_id, lock_state="released", merged_commit=commit)
            board.save_agent_run(task_id, "主控", "merge", "completed", f"已按用户确认合并到本地目标分支。commit={commit}")
            return board.transition(task_id, "completed", actor="approval:user-merge")
        except (StateError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/release-git-lock")
    def release_git_lock(task_id: str) -> dict[str, Any]:
        try:
            task = board.get_task(task_id)
            if task["status"] not in {"failed", "blocked", "cancelled", "completed"} or not task["git_lease"]:
                raise StateError("a Git lock can only be released from a terminal task state")
            if task["git_lease"].get("lock_state") == "released":
                return task
            release_task_lock(task_id, task["git_lease"], locks_root)
            board.update_git_lease(task_id, lock_state="released")
            return board.save_agent_run(task_id, "主控", "recovery", "completed", "已释放范围锁；worktree 保留以便审计或手动清理。")
        except (StateError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return app


app = create_app()
