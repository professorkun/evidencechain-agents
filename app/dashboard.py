"""Local-only FastAPI dashboard. It records approvals but does not execute tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.taskboard import StateError, TaskBoard


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

    return app


app = create_app()
