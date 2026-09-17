from app.codex_bridge import event_from_thread, thread_title


def test_event_from_thread_maps_active_and_idle_sessions() -> None:
    active = {"id": "thread-1", "name": "正在处理", "status": {"type": "active"}, "updatedAt": 10}
    idle = {"id": "thread-1", "name": "已完成", "status": {"type": "idle"}, "updatedAt": 11}

    assert event_from_thread(active).status == "running"
    assert event_from_thread(idle).status == "completed"
    assert event_from_thread(idle).event_id != event_from_thread(active).event_id


def test_thread_title_falls_back_to_preview() -> None:
    assert thread_title({"id": "thread-2", "preview": "第一行\n第二行"}) == "第一行"


def test_bridge_defaults_to_no_historical_backfill() -> None:
    import inspect
    from app.codex_bridge import run_bridge

    assert inspect.signature(run_bridge).parameters["backfill_hours"].default == 0.0


def test_list_threads_requests_repaired_metadata_and_cwd_filter() -> None:
    from app.codex_bridge import CodexAppServer

    server = CodexAppServer()
    calls = []
    server.call = lambda method, params: calls.append((method, params)) or {"data": []}
    assert server.list_threads(cwd={"F:\\GPT"}) == []
    method, params = calls[0]
    assert method == "thread/list"
    assert params["useStateDbOnly"] is False
    assert params["sourceKinds"] == []
    assert params["cwd"] == ["F:\\GPT"]


def test_not_loaded_thread_uses_final_answer_as_completion_signal() -> None:
    active = {"id": "thread-3", "status": {"type": "notLoaded"}, "updatedAt": 20, "turns":[{"items":[{"type":"agentMessage","phase":"commentary"}]}]}
    completed = {"id": "thread-3", "status": {"type": "notLoaded"}, "updatedAt": 21, "turns":[{"items":[{"type":"agentMessage","phase":"final_answer"}]}]}

    assert event_from_thread(active).status == "running"
    assert event_from_thread(completed).status == "completed"
