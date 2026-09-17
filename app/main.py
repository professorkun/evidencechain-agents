from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
SAMPLE_TARGET = ROOT / "sample_target"


class WorkbenchState(TypedDict, total=False):
    task: str
    approved: bool
    plan: str
    risk_review: str
    research: str
    synthesis: str
    execution: dict[str, str]
    final_report: str


def model_mode() -> str:
    return os.getenv("AGENT_MODEL_MODE", "mock").lower()


async def role_reply(role: str, instruction: str, task: str) -> str:
    """Return a role-specific response without allowing tools or file writes."""
    if model_mode() == "mock":
        templates = {
            "方案": "将任务拆成可并行的只读检查、测试与汇总；写入操作保留给后续经确认的阶段。",
            "反方": "风险：并行成员若共享写入范围会互相覆盖；本阶段必须保持只读并记录证据。",
            "调研": "证据收集应限于指定目录、依赖版本和测试结果；不要把推断当成事实。",
        }
        return f"[{role}｜离线模拟] {templates[role]} 任务：{task}"

    if model_mode() != "openai":
        raise ValueError("AGENT_MODEL_MODE must be mock or openai")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise RuntimeError("openai mode requires OPENAI_API_KEY and OPENAI_MODEL")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    response = await client.responses.create(
        model=model,
        input=(
            f"你是{role} Agent。{instruction}\n"
            "只给出可核验的建议，不执行工具，不声称未验证的事实。\n"
            f"任务：{task}"
        ),
    )
    return response.output_text


async def plan_node(state: WorkbenchState) -> dict[str, str]:
    return {"plan": await role_reply("方案", "提出最小可执行方案和任务拆分。", state["task"])}


async def risk_node(state: WorkbenchState) -> dict[str, str]:
    return {"risk_review": await role_reply("反方", "找出风险、依赖和不能并行的部分。", state["task"])}


async def research_node(state: WorkbenchState) -> dict[str, str]:
    return {"research": await role_reply("调研", "说明需要的只读证据和验证方式。", state["task"])}


async def run_discussion(task: str) -> dict[str, str]:
    """Run the three read-only discussion roles concurrently for the task board."""
    plan, risk, research = await asyncio.gather(
        role_reply("方案", "提出最小可执行方案和任务拆分。", task),
        role_reply("反方", "找出风险、依赖和不能并行的部分。", task),
        role_reply("调研", "说明需要的只读证据和验证方式。", task),
    )
    return {"方案 Agent": plan, "反方 Agent": risk, "调研 Agent": research}


def synthesize_node(state: WorkbenchState) -> dict[str, str]:
    return {
        "synthesis": "\n".join(
            [
                "## 讨论汇总",
                state["plan"],
                state["risk_review"],
                state["research"],
                "\n待确认：是否允许进入只读并行验证阶段。",
            ]
        )
    }


def approval_route(state: WorkbenchState) -> str:
    return "execute" if state.get("approved", False) else "report"


def run_command(command: list[str]) -> str:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    output = (completed.stdout + completed.stderr).strip()
    return f"exit={completed.returncode}\n{output}"


async def execute_readonly_node(_: WorkbenchState) -> dict[str, dict[str, str]]:
    """Two independent, non-writing checks run in parallel on the bundled sample."""
    async def inspect() -> tuple[str, str]:
        files = sorted(
            str(path.relative_to(SAMPLE_TARGET))
            for path in SAMPLE_TARGET.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        return "inspect", "files=" + ", ".join(files)

    async def test() -> tuple[str, str]:
        result = await asyncio.to_thread(
            run_command,
            [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "pytest", "tests/test_calculator.py"],
        )
        return "test", result

    results = await asyncio.gather(inspect(), test())
    return {"execution": dict(results)}


def report_node(state: WorkbenchState) -> dict[str, str]:
    execution = state.get("execution")
    status = "已完成两项并行只读验证。" if execution else "停在讨论汇总，尚未获得执行确认。"
    details = "\n\n".join(f"### {name}\n{value}" for name, value in (execution or {}).items())
    return {"final_report": f"# 第一期开关式运行报告\n\n{status}\n\n{state['synthesis']}\n\n{details}"}


def build_graph() -> Any:
    graph = StateGraph(WorkbenchState)
    graph.add_node("plan", plan_node)
    graph.add_node("risk", risk_node)
    graph.add_node("research", research_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("execute", execute_readonly_node)
    graph.add_node("report", report_node)
    graph.add_edge(START, "plan")
    graph.add_edge(START, "risk")
    graph.add_edge(START, "research")
    graph.add_edge(["plan", "risk", "research"], "synthesize")
    graph.add_conditional_edges("synthesize", approval_route, {"execute": "execute", "report": "report"})
    graph.add_edge("execute", "report")
    graph.add_edge("report", END)
    return graph.compile()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the phase-one multi-agent prototype.")
    parser.add_argument("task", help="Task to discuss and optionally validate")
    parser.add_argument("--approve-readonly", action="store_true", help="Allow the two bundled read-only checks after synthesis")
    args = parser.parse_args()

    result = await build_graph().ainvoke({"task": args.task, "approved": args.approve_readonly})
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = RUNS / f"{stamp}-phase1.md"
    report.write_text(result["final_report"], encoding="utf-8")
    print(result["final_report"])
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
