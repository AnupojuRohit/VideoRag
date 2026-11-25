import asyncio

from app.tasks import ingestion


def test_async_task_runner_disposes_database_pool_before_loop_closes(
    monkeypatch,
) -> None:
    loop_events: list[asyncio.AbstractEventLoop] = []

    class FakeEngine:
        async def dispose(self) -> None:
            loop_events.append(asyncio.get_running_loop())

    async def task_result() -> str:
        loop_events.append(asyncio.get_running_loop())
        return "done"

    monkeypatch.setattr(ingestion, "engine", FakeEngine())

    assert ingestion.run_async_task(task_result()) == "done"
    assert ingestion.run_async_task(task_result()) == "done"

    assert loop_events[0] is loop_events[1]
    assert loop_events[2] is loop_events[3]
    assert loop_events[0] is not loop_events[2]
    assert loop_events[0].is_closed()
    assert loop_events[2].is_closed()
