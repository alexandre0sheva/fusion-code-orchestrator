"""End-to-end code review pipeline tests."""

import pytest

from fusion.mcp_server.schemas import ReviewDiffInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.mark.asyncio
async def test_code_review_pipeline(db_path: str) -> None:
    pipeline = create_pipeline(db_path=db_path)
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        " def process(data):\n"
        "-    return data['value']\n"
        "+    return data.value\n"
    )
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content=diff,
        context="Python 3.12 project using dict-based config",
    )
    result = await pipeline.run(ctx)
    assert result.run_id.startswith("run_")
    assert result.final_answer
    assert len(result.panel_results) >= 1
    assert result.final_eval.overall_score > 0


@pytest.mark.asyncio
async def test_mcp_review_tool(db_path: str) -> None:
    tools = FusionTools(db_path=db_path, use_mock=True)
    output = await tools.fusion_review_diff(
        ReviewDiffInput(diff="+ def foo(): pass", context="New function added")
    )
    assert "run_id" in output
    assert "summary" in output
    assert output.get("critical_findings") is not None
