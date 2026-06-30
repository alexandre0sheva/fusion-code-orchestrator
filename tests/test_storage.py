"""Tests for SQLite run storage."""

import pytest

from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType
from fusion.storage.run_store import RunStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.mark.asyncio
async def test_run_persisted(db_path: str) -> None:
    pipeline = create_pipeline(db_path=db_path)
    ctx = PipelineContext(
        task_type=TaskType.PLANNING,
        primary_content="Implement user authentication with OAuth2",
        context="Existing Flask app with SQLAlchemy",
    )
    result = await pipeline.run(ctx)

    store = RunStore(db_path=db_path)
    record = store.get_run(result.run_id)
    assert record is not None
    assert record.status == "completed"
    assert record.task_type == "planning"
    assert record.output_data is not None
    assert len(record.steps) >= 1


def test_create_and_list_runs(db_path: str) -> None:
    store = RunStore(db_path=db_path)
    run_id = store.create_run(
        task_type="test",
        input_data={"foo": "bar"},
        sanitized_input={"foo": "bar"},
    )
    store.complete_run(
        run_id,
        status="completed",
        output_data={"answer": "test"},
        trace={},
        total_cost_usd=0.0,
        total_latency_ms=10.0,
        steps=[],
    )
    runs = store.list_runs(limit=5)
    assert any(r.run_id == run_id for r in runs)
