from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.phase2 import (
    ContractError,
    LockConflict,
    RangeLockRegistry,
    TaskContract,
    create_worktree,
    merge_plan,
    verify_worktree,
)


def contract(*, paths: tuple[str, ...] = ("src",)) -> TaskContract:
    return TaskContract(
        task_id="demo-task",
        base_ref="main",
        allowed_paths=paths,
        test_commands=(("python", "-c", "print('verified')"),),
        acceptance_criteria=("tests pass",),
    )


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True)


def seeded_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "src").mkdir()
    (repository / "src" / "demo.txt").write_text("before\n", encoding="utf-8")
    (repository / "README.md").write_text("sample\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "seed")
    return repository


def test_contract_rejects_parent_escape() -> None:
    with pytest.raises(ContractError):
        contract(paths=("../private",)).validate()


def test_contract_json_round_trip() -> None:
    assert TaskContract.from_json(contract().to_json()) == contract()


def test_range_locks_allow_disjoint_ranges_and_block_overlap(tmp_path: Path) -> None:
    registry = RangeLockRegistry(tmp_path / "state")
    registry.acquire("task-one", ("src/api",))
    registry.acquire("task-two", ("docs",))
    with pytest.raises(LockConflict, match="src"):
        registry.acquire("task-three", ("src",))
    registry.release("task-one")
    assert registry.active() == {"task-two": ["docs"]}


def test_worktree_verification_and_merge_plan_are_non_merging(tmp_path: Path) -> None:
    repository = seeded_repository(tmp_path)
    lease = create_worktree(repository, contract(), "executor-a", tmp_path / "worktrees")
    (lease.path / "src" / "demo.txt").write_text("after\n", encoding="utf-8")

    result = verify_worktree(lease.path, contract(), lease.base_commit)
    plan = merge_plan(lease, result)

    assert result.passed
    assert result.changed_files == ("src/demo.txt",)
    assert "no merge performed" in plan
    assert "eligible for user-approved merge" in plan
    assert (repository / "src" / "demo.txt").read_text(encoding="utf-8") == "before\n"


def test_verifier_blocks_changes_outside_the_contract(tmp_path: Path) -> None:
    repository = seeded_repository(tmp_path)
    lease = create_worktree(repository, contract(), "executor-a", tmp_path / "worktrees")
    (lease.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = verify_worktree(lease.path, contract(), lease.base_commit)

    assert not result.passed
    assert result.outside_scope == ("README.md",)
