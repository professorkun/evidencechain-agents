"""Read-only Codex app-server bridge for the local workbench."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class BridgeEvent:
    external_task_id: str
    title: str
    event_id: str
    event_type: str
    status: str
    message: str


def thread_title(thread: dict[str, Any]) -> str:
    name = str(thread.get("name") or "").strip()
    if name:
        return name
    preview = str(thread.get("preview") or "").splitlines()
    return (preview[0].strip() if preview else "") or f"Codex 任务 {thread.get('id', 'unknown')}"


def event_from_thread(thread: dict[str, Any]) -> BridgeEvent | None:
    thread_id = str(thread.get("id") or "").strip()
    if not thread_id:
        return None
    status_type = str((thread.get("status") or {}).get("type") or "").lower()
    if status_type in {"notloaded", "not_loaded"}:
        turns = thread.get("turns") or []
        last_items = (turns[-1].get("items") or []) if turns else []
        finished = any(item.get("type") == "agentMessage" and item.get("phase") == "final_answer" for item in last_items)
        status_type = "idle" if finished else "active"
    updated = str(thread.get("updatedAt") or thread.get("updated_at") or "0")
    if status_type == "active":
        return BridgeEvent(thread_id, thread_title(thread), f"thread-{thread_id}-running", "task.started", "running", "Codex 会话正在运行")
    if status_type in {"idle", "notloaded", "not_loaded", "completed"}:
        return BridgeEvent(thread_id, thread_title(thread), f"thread-{thread_id}-completed", "task.completed", "completed", "Codex 会话已结束；已同步会话状态")
    return BridgeEvent(thread_id, thread_title(thread), f"thread-{thread_id}-{status_type or 'unknown'}", "sync.observed", "", f"Codex 会话状态：{status_type or '未知'}")


class CodexAppServer:
    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0

    def start(self) -> None:
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.call("initialize", {"clientInfo": {"name": "multi-agent-workbench", "title": "Multi-Agent Workbench", "version": "0.1"}})

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("Codex app-server is not running")
        self.request_id += 1
        self.process.stdin.write(json.dumps({"id": self.request_id, "method": method, "params": params}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Codex app-server closed its output")
            message = json.loads(line)
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result", {})

    def list_threads(self, limit: int = 100, cwd: set[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "archived": False,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "useStateDbOnly": False,
            "sourceKinds": [],
        }
        if cwd:
            params["cwd"] = sorted(cwd)
        result = self.call("thread/list", params)
        return list(result.get("data") or [])

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self.call("thread/read", {"threadId": thread_id, "includeTurns": True}).get("thread", {})


def post_event(base_url: str, event: BridgeEvent, *, timeout: float = 5.0) -> dict[str, Any]:
    payload = {
        "external_task_id": event.external_task_id,
        "title": event.title,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "status": event.status or None,
        "message": event.message,
    }
    request = Request(
        f"{base_url.rstrip('/')}/api/events",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_bridge(
    *,
    base_url: str,
    roots: set[str],
    poll_seconds: float = 3.0,
    once: bool = False,
    backfill_hours: float = 0.0,
    thread_ids: set[str] | None = None,
    app_server_factory: Callable[[], CodexAppServer] = CodexAppServer,
) -> None:
    server = app_server_factory()
    server.start()
    baseline = True
    last_seen: dict[str, str] = {}
    cutoff = time.time() - backfill_hours * 3600
    try:
        while True:
            for thread in server.list_threads(cwd=roots):
                thread_id = str(thread.get("id") or "")
                if thread_ids and thread_id not in thread_ids:
                    continue
                updated = float(thread.get("updatedAt") or thread.get("updated_at") or 0)
                if not thread_id or (baseline and backfill_hours <= 0):
                    if thread_id:
                        last_seen[thread_id] = str(updated)
                    continue
                if baseline and updated < cutoff:
                    last_seen[thread_id] = str(updated)
                    continue
                if not baseline and updated <= float(last_seen.get(thread_id, "0")):
                    continue
                cwd = str(thread.get("cwd") or "").rstrip("\\/").lower()
                if roots and cwd not in {root.rstrip("\\/").lower() for root in roots}:
                    continue
                if str((thread.get("status") or {}).get("type") or "").lower() in {"notloaded", "not_loaded"}:
                    thread = server.read_thread(thread_id)
                event = event_from_thread(thread)
                if event:
                    post_event(base_url, event)
                last_seen[thread_id] = str(updated)
            baseline = False
            if once:
                return
            time.sleep(poll_seconds)
    finally:
        server.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read Codex app-server thread status into the local workbench")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--cwd", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--backfill-hours", type=float, default=0.0)
    parser.add_argument("--thread-id", action="append", default=[])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_bridge(base_url=args.url, roots=set(args.cwd), poll_seconds=args.poll_seconds, once=args.once, backfill_hours=args.backfill_hours, thread_ids=set(args.thread_id) or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
