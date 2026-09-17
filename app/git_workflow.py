"""Task-board adapter around the phase-two isolated Git safeguards."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.phase2 import ContractError, RangeLockRegistry, TaskContract, VerificationResult, WorktreeLease, create_worktree, merge_plan, run_git, verify_worktree


def contract_from_task(task: dict[str, Any]) -> tuple[Path, str, TaskContract]:
    data = task["contract"]
    repository = Path(str(data.get("repository", ""))).expanduser()
    if not repository.is_absolute() or not repository.is_dir():
        raise ContractError("repository must be an existing absolute local path")
    role = str(data.get("executor_role", "executor-a"))
    contract = TaskContract(
        task_id=task["id"],
        base_ref=str(data.get("base_ref", "main")),
        allowed_paths=tuple(str(item) for item in data.get("allowed_paths", [])),
        test_commands=tuple(tuple(str(part) for part in command) for command in data.get("test_commands", [])),
        acceptance_criteria=tuple(str(item) for item in data.get("acceptance_criteria", [])),
    ).validate()
    return repository.resolve(), role, contract


def registry_for(repository: Path, locks_root: Path) -> RangeLockRegistry:
    key = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()[:16]
    return RangeLockRegistry(locks_root / key)


def prepare_task_worktree(task: dict[str, Any], worktrees_root: Path, locks_root: Path) -> tuple[WorktreeLease, TaskContract]:
    repository, role, contract = contract_from_task(task)
    registry = registry_for(repository, locks_root)
    registry.acquire(contract.task_id, contract.allowed_paths)
    try:
        lease = create_worktree(repository, contract, role, worktrees_root)
    except Exception:
        registry.release(contract.task_id)
        raise
    return lease, contract


def serialize_lease(lease: WorktreeLease, contract: TaskContract, repository: Path) -> dict[str, Any]:
    return {
        "repository": str(repository),
        "worktree": str(lease.path),
        "branch": lease.branch,
        "base_commit": lease.base_commit,
        "role": lease.role,
        "base_ref": contract.base_ref,
        "allowed_paths": list(contract.allowed_paths),
        "test_commands": [list(command) for command in contract.test_commands],
        "acceptance_criteria": list(contract.acceptance_criteria),
        "lock_state": "active",
    }


def lease_from_record(task_id: str, record: dict[str, Any]) -> tuple[WorktreeLease, TaskContract, Path]:
    repository = Path(record["repository"])
    contract = TaskContract(
        task_id=task_id,
        base_ref=str(record.get("base_ref", "main")),
        allowed_paths=tuple(record["allowed_paths"]),
        test_commands=tuple(tuple(command) for command in record["test_commands"]),
        acceptance_criteria=tuple(record["acceptance_criteria"]),
    ).validate()
    lease = WorktreeLease(task_id, record["role"], record["branch"], record["base_commit"], Path(record["worktree"]))
    return lease, contract, repository


def executor_brief(task: dict[str, Any], record: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# 执行 Agent 任务说明（受限写入）",
            f"任务：{task['title']}",
            f"工作目录：{record['worktree']}",
            f"分支：{record['branch']}",
            "允许修改：" + ", ".join(record["allowed_paths"]),
            "禁止：修改允许范围外文件、切换分支、合并、推送、SSH 或访问 NAS。",
            "完成后：运行合同测试；仅在当前 worktree 提交变更；把提交哈希和测试输出交给验证 Agent。",
        ]
    )


def verify_task_worktree(task_id: str, record: dict[str, Any]) -> tuple[VerificationResult, str]:
    lease, contract, _ = lease_from_record(task_id, record)
    result = verify_worktree(lease.path, contract, lease.base_commit)
    return result, merge_plan(lease, result)


def verified_worktree_commit(task_id: str, record: dict[str, Any]) -> str:
    lease, _, _ = lease_from_record(task_id, record)
    if run_git(lease.path, "status", "--porcelain"):
        raise ContractError("worktree has uncommitted changes; execution Agent must commit before verification")
    commit = run_git(lease.path, "rev-parse", "HEAD")
    if commit == lease.base_commit:
        raise ContractError("worktree has no committed changes beyond the task base")
    return commit


def merge_verified_task(task_id: str, record: dict[str, Any]) -> str:
    lease, _, repository = lease_from_record(task_id, record)
    if run_git(repository, "status", "--porcelain"):
        raise ContractError("repository has uncommitted changes; refuse to merge")
    verified_commit = str(record.get("verified_commit", ""))
    if not verified_commit or run_git(lease.path, "rev-parse", "HEAD") != verified_commit:
        raise ContractError("worktree changed after verification; verify again before merging")
    run_git(repository, "merge", "--ff-only", lease.branch)
    return run_git(repository, "rev-parse", "HEAD")


def release_task_lock(task_id: str, record: dict[str, Any], locks_root: Path) -> None:
    repository = Path(record["repository"])
    registry_for(repository, locks_root).release(task_id)
