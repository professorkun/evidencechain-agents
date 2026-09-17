"""Durable, local-only task state for the phase-three dashboard."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STATUSES = (
    "draft",
    "deliberating",
    "awaiting_approval",
    "queued",
    "running",
    "verifying",
    "awaiting_merge",
    "completed",
    "blocked",
    "failed",
    "cancelled",
    "interrupted_needs_review",
)

TRANSITIONS = {
    "draft": {"deliberating", "cancelled"},
    "deliberating": {"awaiting_approval", "blocked", "cancelled"},
    "awaiting_approval": {"queued", "cancelled"},
    "queued": {"running", "blocked", "cancelled"},
    "running": {"verifying", "failed", "blocked", "interrupted_needs_review"},
    "verifying": {"awaiting_merge", "completed", "failed", "blocked"},
    "awaiting_merge": {"completed", "cancelled", "blocked"},
    "blocked": {"awaiting_approval", "cancelled"},
    "failed": {"awaiting_approval", "cancelled"},
    "interrupted_needs_review": {"awaiting_approval", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class StateError(ValueError):
    """A requested task transition is not allowed."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskBoard:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    contract_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS agent_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                """
            )

    def create_task(self, title: str, contract: dict[str, Any] | None = None) -> dict[str, Any]:
        if not title.strip():
            raise StateError("task title is required")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        timestamp = now()
        payload = json.dumps(contract or {}, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, title.strip(), "draft", payload, timestamp, timestamp),
            )
            self._event(connection, task_id, "controller", "created", {"status": "draft"})
        return self.get_task(task_id)

    def _event(self, connection: sqlite3.Connection, task_id: str, actor: str, event_type: str, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO events (task_id, actor, event_type, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, actor, event_type, json.dumps(detail, ensure_ascii=False), now()),
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown task: {task_id}")
            events = connection.execute(
                "SELECT actor, event_type, detail_json, created_at FROM events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            reports = connection.execute(
                "SELECT role, status, content, created_at FROM agent_reports WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "contract": json.loads(row["contract_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "events": [
                {"actor": event["actor"], "type": event["event_type"], "detail": json.loads(event["detail_json"]), "at": event["created_at"]}
                for event in events
            ],
            "agent_reports": [
                {"role": report["role"], "status": report["status"], "content": report["content"], "at": report["created_at"]}
                for report in reports
            ],
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["id"]) for row in rows]

    def transition(self, task_id: str, target_status: str, *, actor: str = "controller") -> dict[str, Any]:
        if target_status not in STATUSES:
            raise StateError(f"unknown status: {target_status}")
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown task: {task_id}")
            current_status = row["status"]
            if target_status not in TRANSITIONS[current_status]:
                raise StateError(f"cannot transition from {current_status} to {target_status}")
            timestamp = now()
            connection.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (target_status, timestamp, task_id))
            self._event(connection, task_id, actor, "transition", {"from": current_status, "to": target_status})
        return self.get_task(task_id)

    def approve(self, task_id: str, action: str, *, approver: str = "user") -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "awaiting_approval":
            raise StateError("only tasks awaiting approval can be approved")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals (task_id, action, approver, created_at) VALUES (?, ?, ?, ?)",
                (task_id, action, approver, now()),
            )
        return self.transition(task_id, "queued", actor=f"approval:{approver}")

    def save_agent_reports(self, task_id: str, reports: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "deliberating":
            raise StateError("discussion can only run while a task is deliberating")
        if task["agent_reports"]:
            raise StateError("discussion reports already exist for this task")
        with self._connect() as connection:
            for role, content in reports.items():
                connection.execute(
                    "INSERT INTO agent_reports (task_id, role, status, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, role, "completed", content, now()),
                )
                self._event(connection, task_id, f"agent:{role}", "discussion_completed", {"role": role})
        return self.get_task(task_id)
