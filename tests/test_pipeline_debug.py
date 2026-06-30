"""End-to-end debug pipeline tests."""

import pytest

from fusion.mcp_server.schemas import DebugErrorInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.mark.asyncio
async def test_debug_pipeline(db_path: str) -> None:
    pipeline = create_pipeline(db_path=db_path)
    ctx = PipelineContext(
        task_type=TaskType.DEBUGGING,
        primary_content="Error: ConnectionPoolExhausted\nTimeout waiting for connection",
        context="PostgreSQL async pool, max_size=5",
    )
    result = await pipeline.run(ctx)
    assert result.run_id
    assert "mock" in result.final_answer.lower() or "connection" in result.final_answer.lower()
    assert result.context_eval.sufficient


@pytest.mark.asyncio
async def test_mcp_debug_tool(db_path: str) -> None:
    tools = FusionTools(db_path=db_path, use_mock=True)
    output = await tools.fusion_debug_error(
        DebugErrorInput(
            error_message="NullPointerException at line 42",
            stack_trace="at com.example.Service.run(Service.java:42)",
        )
    )
    assert "most_likely_causes" in output
    assert output.get("run_id")
