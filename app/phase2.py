"""Git-isolated execution primitives for phase two.

This module deliberately prepares and verifies work; it never merges into main.
The caller must keep user approval outside of this module before invoking it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


class ContractError(ValueError):
    """A task contract is incomplete or unsafe."""


class LockConflict(RuntimeError):
    """A requested write range overlaps an active task."""


class GitOperationError(RuntimeError):
    """A Git operation did not complete successfully."""


def normalise_resource(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if not value or path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise ContractError(f"unsafe allowed path: {value!r}")
    return str(path).rstrip("/")


def resources_overlap(left: str, right: str) -> bool:
    """Treat a listed directory as owning all of its descendants."""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    base_ref: str
    allowed_paths: tuple[str, ...]
    test_commands: tuple[tuple[str, ...], ...]
    acceptance_criteria: tuple[str, ...]

    def validate(self) -> "TaskContract":
        if not TASK_ID_PATTERN.fullmatch(self.task_id):
            raise ContractError("task_id must be lowercase letters, digits, and hyphens")
        if not self.base_ref.strip():
            raise ContractError("base_ref is required")
        paths = tuple(normalise_resource(path) for path in self.allowed_paths)
        if not paths:
            raise ContractError("at least one allowed path is required")
        if len(set(paths)) != len(paths):
            raise ContractError("allowed paths must be unique")
        if not self.test_commands or any(not command for command in self.test_commands):
            raise ContractError("at least one non-empty test command is required")
        if not self.acceptance_criteria or any(not item.strip() for item in self.acceptance_criteria):
            raise ContractError("at least one acceptance criterion is required")
        return TaskContract(self.task_id, self.base_ref, paths, self.test_commands, self.acceptance_criteria)

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "TaskContract":
        data = json.loads(text)
        return cls(
            task_id=str(data["task_id"]),
            base_ref=str(data["base_ref"]),
            allowed_paths=tuple(str(item) for item in data["allowed_paths"]),
            test_commands=tuple(tuple(str(part) for part in command) for command in data["test_commands"]),
            acceptance_criteria=tuple(str(item) for item in data["acceptance_criteria"]),
        ).validate()


@dataclass(frozen=True)
class WorktreeLease:
    task_id: str
    role: str
    branch: str
    base_commit: str
    path: Path


@dataclass(frozen=True)
class VerificationResult:
    changed_files: tuple[str, ...]
    outside_scope: tuple[str, ...]
    command_results: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.outside_scope and all(result.startswith("exit=0") for result in self.command_results)


class RangeLockRegistry:
    """Small local registry with atomic updates and explicit release/recovery."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root
        self.registry_path = state_root / "range-locks.json"
        self.guard_path = state_root / "range-locks.guard"

    @contextmanager
    def _guard(self) -> Iterator[None]:
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.guard_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise LockConflict("lock registry is busy; inspect or recover it before retrying") from error
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            self.guard_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, list[str]]:
        if not self.registry_path.exists():
            return {}
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return {str(key): [normalise_resource(item) for item in value] for key, value in data.items()}

    def _write(self, records: dict[str, list[str]]) -> None:
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.registry_path)

    def acquire(self, task_id: str, resources: tuple[str, ...]) -> None:
        requested = [normalise_resource(item) for item in resources]
        with self._guard():
            records = self._read()
            for active_task, active_resources in records.items():
                if active_task == task_id:
                    continue
                overlaps = [
                    f"{requested_item} ↔ {active_item} ({active_task})"
                    for requested_item in requested
                    for active_item in active_resources
                    if resources_overlap(requested_item, active_item)
                ]
                if overlaps:
                    raise LockConflict("write range conflict: " + "; ".join(overlaps))
            records[task_id] = requested
            self._write(records)

    def release(self, task_id: str) -> None:
        with self._guard():
            records = self._read()
            if task_id in records:
                del records[task_id]
                self._write(records)

    def active(self) -> dict[str, list[str]]:
        return self._read()

    def recover_guard(self, *, older_than_seconds: float) -> bool:
        """Remove only a stale registry guard; active task locks remain untouched."""
        if not self.guard_path.exists():
            return False
        age = time.time() - self.guard_path.stat().st_mtime
        if age < older_than_seconds:
            raise LockConflict("registry guard is not stale")
        self.guard_path.unlink()
        return True


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode:
        detail = (completed.stdout + completed.stderr).strip()
        raise GitOperationError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def assert_clean(repository: Path) -> None:
    if run_git(repository, "status", "--porcelain"):
        raise GitOperationError("repository has uncommitted changes; refuse to create a task worktree")


def create_worktree(
    repository: Path,
    contract: TaskContract,
    role: str,
    worktree_root: Path,
) -> WorktreeLease:
    """Create one task branch from the recorded base without touching main."""
    contract = contract.validate()
    if not ROLE_PATTERN.fullmatch(role):
        raise ContractError("role must be lowercase letters, digits, and hyphens")
    assert_clean(repository)
    base_commit = run_git(repository, "rev-parse", "--verify", f"{contract.base_ref}^{{commit}}")
    branch = f"codex/task-{contract.task_id}-{role}"
    target = worktree_root / contract.task_id / role
    if target.exists():
        raise GitOperationError(f"worktree target already exists: {target}")
    if run_git(repository, "branch", "--list", branch):
        raise GitOperationError(f"branch already exists: {branch}")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(repository, "worktree", "add", "-b", branch, str(target), base_commit)
    return WorktreeLease(contract.task_id, role, branch, base_commit, target)


def changed_files(worktree: Path, base_commit: str) -> tuple[str, ...]:
    output = run_git(worktree, "diff", "--name-only", base_commit, "--")
    return tuple(line for line in output.splitlines() if line)


def resolve_test_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """Replace the portable contract token with this controller's interpreter."""
    return tuple(sys.executable if part == "{python}" else part for part in command)


def verify_worktree(worktree: Path, contract: TaskContract, base_commit: str) -> VerificationResult:
    contract = contract.validate()
    changed = changed_files(worktree, base_commit)
    outside = tuple(
        path
        for path in changed
        if not any(resources_overlap(path, allowed) for allowed in contract.allowed_paths)
    )
    command_results: list[str] = []
    for command in contract.test_commands:
        completed = subprocess.run(
            resolve_test_command(command), cwd=worktree, text=True, capture_output=True, check=False, timeout=60
        )
        output = (completed.stdout + completed.stderr).strip()
        command_results.append(f"exit={completed.returncode}\n{output}")
    return VerificationResult(changed, outside, tuple(command_results))


def merge_plan(lease: WorktreeLease, verification: VerificationResult) -> str:
    status = "eligible for user-approved merge" if verification.passed else "blocked; do not merge"
    changed = ", ".join(verification.changed_files) or "no files changed"
    outside = ", ".join(verification.outside_scope) or "none"
    return "\n".join(
        [
            "# Phase 2 merge plan (no merge performed)",
            f"branch: {lease.branch}",
            f"base: {lease.base_commit}",
            f"changed files: {changed}",
            f"outside allowed range: {outside}",
            f"status: {status}",
            "next step: obtain explicit user confirmation before a single controller merges into main.",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 Git-isolated execution safeguards.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-contract", help="Validate a task contract without changing Git state.")
    validate.add_argument("contract", type=Path)
    prepare = subparsers.add_parser("prepare", help="Create one isolated task worktree after explicit approval.")
    prepare.add_argument("contract", type=Path)
    prepare.add_argument("--repository", type=Path, required=True)
    prepare.add_argument("--role", required=True)
    prepare.add_argument("--worktree-root", type=Path, required=True)
    prepare.add_argument(
        "--approve-worktree",
        action="store_true",
        help="Required because this creates a Git branch and worktree.",
    )
    args = parser.parse_args()
    contract = TaskContract.from_json(args.contract.read_text(encoding="utf-8"))
    if args.command == "validate-contract":
        print(contract.to_json(), end="")
        return 0
    if not args.approve_worktree:
        parser.error("prepare requires --approve-worktree")
    lease = create_worktree(args.repository, contract, args.role, args.worktree_root)
    print(
        json.dumps(
            {
                "task_id": lease.task_id,
                "role": lease.role,
                "branch": lease.branch,
                "base_commit": lease.base_commit,
                "path": str(lease.path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
