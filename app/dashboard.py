"""Local-only FastAPI dashboard. It records approvals but does not execute tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.taskboard import StateError, TaskBoard
from app.main import execute_readonly_node, run_discussion


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "taskboard.sqlite3"
STATIC_PAGE = ROOT / "app" / "static" / "dashboard.html"


class CreateTaskRequest(BaseModel):
    title: str
    contract: dict[str, Any] = {}


class ApprovalRequest(BaseModel):
    action: str = "approve_execution"
    approver: str = "user"


def create_app(database_path: Path = DEFAULT_DATABASE) -> FastAPI:
    board = TaskBoard(database_path)
    app = FastAPI(title="Multi-Agent Workbench", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_PAGE)

    @app.get("/api/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return board.list_tasks()

    @app.post("/api/tasks")
    def create_task(request: CreateTaskRequest) -> dict[str, Any]:
        try:
            return board.create_task(request.title, request.contract)
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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

    return app


app = create_app()
