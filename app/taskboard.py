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

VISIBLE_HISTORY_STATUSES = {"completed", "failed", "blocked", "cancelled", "interrupted_needs_review"}

TRANSITIONS = {
    "draft": {"deliberating", "cancelled"},
    "deliberating": {"awaiting_approval", "blocked", "cancelled"},
    "awaiting_approval": {"queued", "cancelled"},
    "queued": {"running", "blocked", "cancelled"},
    "running": {"verifying", "failed", "blocked", "cancelled", "interrupted_needs_review"},
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
                    source TEXT NOT NULL DEFAULT 'workbench',
                    external_task_id TEXT,
                    sync_state TEXT NOT NULL DEFAULT 'local',
                    control_mode TEXT NOT NULL DEFAULT 'workbench',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    external_event_id TEXT,
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
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS git_leases (
                    task_id TEXT PRIMARY KEY,
                    lease_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                """
            )
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "source" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'workbench'")
            if "external_task_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN external_task_id TEXT")
            if "sync_state" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'local'")
            if "control_mode" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN control_mode TEXT NOT NULL DEFAULT 'workbench'")
            connection.execute("UPDATE tasks SET control_mode = 'observe_only' WHERE source = 'codex-bridge'")
            event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            if "external_event_id" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN external_event_id TEXT")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_external_id ON tasks(external_task_id) WHERE external_task_id IS NOT NULL")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_external_id ON events(task_id, external_event_id) WHERE external_event_id IS NOT NULL")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(agent_reports)")}
            if "source" not in columns:
                connection.execute("ALTER TABLE agent_reports ADD COLUMN source TEXT NOT NULL DEFAULT 'dashboard-mock'")

    def create_task(
        self,
        title: str,
        contract: dict[str, Any] | None = None,
        *,
        source: str = "workbench",
        external_task_id: str | None = None,
        sync_state: str = "local",
        control_mode: str = "workbench",
    ) -> dict[str, Any]:
        if not title.strip():
            raise StateError("task title is required")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        timestamp = now()
        payload = json.dumps(contract or {}, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks (id, title, status, contract_json, source, external_task_id, sync_state, control_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title.strip(), "draft", payload, source, external_task_id, sync_state, control_mode, timestamp, timestamp),
            )
            self._event(connection, task_id, "controller", "created", {"status": "draft"})
        return self.get_task(task_id)

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        actor: str,
        event_type: str,
        detail: dict[str, Any],
        external_event_id: str | None = None,
    ) -> bool:
        if external_event_id:
            existing = connection.execute(
                "SELECT 1 FROM events WHERE task_id = ? AND external_event_id = ?",
                (task_id, external_event_id),
            ).fetchone()
            if existing:
                return False
        connection.execute(
            "INSERT INTO events (task_id, actor, event_type, detail_json, external_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, actor, event_type, json.dumps(detail, ensure_ascii=False), external_event_id, now()),
        )
        return True

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown task: {task_id}")
            events = connection.execute(
                "SELECT actor, event_type, detail_json, created_at FROM events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            reports = connection.execute(
                "SELECT role, status, content, source, created_at FROM agent_reports WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            runs = connection.execute(
                "SELECT role, phase, status, content, created_at FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            lease = connection.execute("SELECT lease_json FROM git_leases WHERE task_id = ?", (task_id,)).fetchone()
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "contract": json.loads(row["contract_json"]),
            "source": row["source"],
            "external_task_id": row["external_task_id"],
            "sync_state": row["sync_state"],
            "control_mode": row["control_mode"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "events": [
                {"actor": event["actor"], "type": event["event_type"], "detail": json.loads(event["detail_json"]), "at": event["created_at"]}
                for event in events
            ],
            "agent_reports": [
                {"role": report["role"], "status": report["status"], "content": report["content"], "source": report["source"], "at": report["created_at"]}
                for report in reports
            ],
            "agent_runs": [
                {"role": run["role"], "phase": run["phase"], "status": run["status"], "content": run["content"], "at": run["created_at"]}
                for run in runs
            ],
            "git_lease": json.loads(lease["lease_json"]) if lease else None,
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["id"]) for row in rows]

    def delete_task(self, task_id: str) -> None:
        """Permanently remove a local task and its audit records."""
        with self._connect() as connection:
            row = connection.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown task: {task_id}")
            for table in ("events", "approvals", "agent_reports", "agent_runs", "git_leases"):
                connection.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def ingest_external_event(
        self,
        *,
        external_task_id: str,
        title: str,
        event_id: str,
        event_type: str,
        status: str | None = None,
        message: str = "",
        output: str = "",
    ) -> dict[str, Any]:
        """Accept an idempotent event from a Codex-side bridge."""
        if not external_task_id.strip() or not event_id.strip() or not event_type.strip():
            raise StateError("external_task_id, event_id and event_type are required")
        target = status or {
            "task.started": "running",
            "discussion.started": "deliberating",
            "approval.required": "awaiting_approval",
            "execution.started": "running",
            "verification.started": "verifying",
            "task.completed": "completed",
            "task.failed": "failed",
            "task.cancelled": "cancelled",
            "task.interrupted": "interrupted_needs_review",
        }.get(event_type)
        if target and target not in STATUSES:
            raise StateError(f"unknown external status: {target}")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE external_task_id = ?", (external_task_id,)
            ).fetchone()
            if row is None:
                timestamp = now()
                task_id = f"codex-{uuid.uuid4().hex[:8]}"
                connection.execute(
                    "INSERT INTO tasks (id, title, status, contract_json, source, external_task_id, sync_state, control_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, title.strip() or f"Codex 任务 {external_task_id}", target or "draft", "{}", "codex-bridge", external_task_id, "live", "observe_only", timestamp, timestamp),
                )
            else:
                task_id = row["id"]
                current = row["status"]
                if target and target != current:
                    workbench_task = row["source"] == "workbench"
                    allowed = target in TRANSITIONS.get(current, set())
                    if workbench_task and not allowed:
                        raise StateError(f"external event cannot bypass workbench gate: {current} to {target}")
                    connection.execute(
                        "UPDATE tasks SET status = ?, sync_state = 'live', updated_at = ? WHERE id = ?",
                        (target, now(), task_id),
                    )
                    self._event(connection, task_id, "codex-bridge", "transition", {"from": current, "to": target}, external_event_id=f"{event_id}:transition")
                else:
                    connection.execute("UPDATE tasks SET sync_state = 'live', updated_at = ? WHERE id = ?", (now(), task_id))
            detail = {"external_task_id": external_task_id, "status": target, "message": message, "output": output}
            self._event(connection, task_id, "codex-bridge", event_type, detail, external_event_id=event_id)
        return self.get_task(task_id)

    def transition(self, task_id: str, target_status: str, *, actor: str = "controller") -> dict[str, Any]:
        if target_status not in STATUSES:
            raise StateError(f"unknown status: {target_status}")
        with self._connect() as connection:
            row = connection.execute("SELECT status, control_mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateError(f"unknown task: {task_id}")
            if row["control_mode"] == "observe_only":
                raise StateError("Codex observed tasks are read-only in the workbench")
            current_status = row["status"]
            if target_status not in TRANSITIONS[current_status]:
                raise StateError(f"cannot transition from {current_status} to {target_status}")
            if target_status == "queued":
                approval = connection.execute(
                    "SELECT 1 FROM approvals WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if approval is None:
                    raise StateError("entering the execution queue requires explicit user approval")
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

    def cancel(self, task_id: str, *, actor: str = "user") -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["control_mode"] == "observe_only":
            raise StateError("Codex observed tasks are read-only in the workbench")
        return self.transition(task_id, "cancelled", actor=f"{actor}:cancel")

    def retry(self, task_id: str, *, actor: str = "user") -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["control_mode"] == "observe_only":
            raise StateError("Codex observed tasks are read-only in the workbench")
        if task["status"] not in {"failed", "blocked", "interrupted_needs_review"}:
            raise StateError("only failed, blocked, or interrupted tasks can be retried")
        with self._connect() as connection:
            self._event(connection, task_id, actor, "retry_requested", {"from": task["status"]})
        return self.transition(task_id, "awaiting_approval", actor=f"{actor}:retry")

    def save_agent_reports(self, task_id: str, reports: dict[str, str], *, source: str = "dashboard-mock") -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "deliberating":
            raise StateError("discussion can only run while a task is deliberating")
        if task["agent_reports"]:
            raise StateError("discussion reports already exist for this task")
        with self._connect() as connection:
            for role, content in reports.items():
                connection.execute(
                    "INSERT INTO agent_reports (task_id, role, status, content, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, role, "completed", content, source, now()),
                )
                self._event(connection, task_id, f"agent:{role}", "discussion_completed", {"role": role, "source": source})
        return self.get_task(task_id)

    def save_agent_run(self, task_id: str, role: str, phase: str, status: str, content: str) -> dict[str, Any]:
        self.get_task(task_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO agent_runs (task_id, role, phase, status, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, role, phase, status, content, now()),
            )
            self._event(connection, task_id, f"agent:{role}", f"{phase}_{status}", {"phase": phase})
        return self.get_task(task_id)

    def save_git_lease(self, task_id: str, lease: dict[str, Any]) -> dict[str, Any]:
        self.get_task(task_id)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO git_leases (task_id, lease_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET lease_json = excluded.lease_json, updated_at = excluded.updated_at",
                (task_id, json.dumps(lease, ensure_ascii=False), now()),
            )
            self._event(connection, task_id, "controller", "git_lease_saved", {"branch": lease["branch"]})
        return self.get_task(task_id)

    def update_git_lease(self, task_id: str, **changes: Any) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task["git_lease"]:
            raise StateError("task has no Git worktree lease")
        lease = {**task["git_lease"], **changes}
        return self.save_git_lease(task_id, lease)
