import asyncio

from app.main import build_graph


def test_discussion_stops_without_approval() -> None:
    result = asyncio.run(build_graph().ainvoke({"task": "只讨论", "approved": False}))
    assert "讨论汇总" in result["final_report"]
    assert "execution" not in result


def test_approved_run_executes_two_checks() -> None:
    result = asyncio.run(build_graph().ainvoke({"task": "验证", "approved": True}))
    assert set(result["execution"]) == {"inspect", "test"}
    assert "exit=0" in result["execution"]["test"]
