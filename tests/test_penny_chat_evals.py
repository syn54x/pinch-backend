"""M9 CP4: chat golden-task eval machinery (issue #58).

CI asserts the machinery: the trajectory evaluator's three failure modes
(wrong tool, unapproved write, ungrounded number), dataset hygiene, and
one sandboxed FunctionModel run through the real capability stack. The
judge and live trajectories are local eval runs, never CI.
"""

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case, Dataset

from pinch_backend.api.app import create_app
from pinch_backend.penny.agents import chat_agent
from pinch_backend.penny.evals_chat import (
    ChatTrajectory,
    chat_task,
    numbers_grounded,
    provision_sandbox,
)


def test_grounding_heuristic_is_minor_units_aligned() -> None:
    assert numbers_grounded("You spent $42.00 there", "amount_minor: 4200")
    assert numbers_grounded("Total: $1,234.56", "123456 USD")
    assert not numbers_grounded("You spent $99.10", "amount_minor: 4200")
    assert numbers_grounded("No numbers here", "")  # nothing to ground


def test_grounding_ignores_years_and_bare_dates() -> None:
    """ "No transactions in July 2026" is an honest answer, not a claim
    about money — bare years never count as fabricated numbers."""
    assert numbers_grounded("You had no Whole Foods transactions in July 2026.", "[]")
    assert numbers_grounded("Between 2026-07-01 and 2026-07-31, nothing.", "[]")


def test_grounding_accepts_shown_work_arithmetic() -> None:
    """A total computed from grounded addends is arithmetic, not
    fabrication — provided the addends are shown."""
    tool_text = "amount_minor: -12734 ... amount_minor: -8121"
    assert numbers_grounded("$127.34 + $81.21 = $208.55 total", tool_text)
    # The bare total without its addends is indistinguishable from an
    # invented number: still ungrounded.
    assert not numbers_grounded("You spent $208.55", tool_text)
    # And a wrong "sum" over grounded addends stays wrong.
    assert not numbers_grounded("$127.34 + $81.21 = $999.99", tool_text)


async def _evaluate_one(output: dict, metadata: dict):
    dataset = Dataset(
        name="probe",
        cases=[Case(name="c", inputs={}, metadata=metadata)],
        evaluators=[ChatTrajectory()],
    )

    async def task(inputs: dict) -> dict:
        return output

    report = await dataset.evaluate(task, progress=False)
    case = report.cases[0]
    return case.scores["trajectory"].value, case.assertions


GOOD = {
    "answer": "You spent $42.00",
    "tools_called": ["spending_report"],
    "paused": False,
    "write_executed": False,
    "tool_text": "total 4200",
}


async def test_trajectory_passes_the_good_path() -> None:
    score, _ = await _evaluate_one(GOOD, {"expected_tools": ["spending_report"]})
    assert score == 1.0


async def test_trajectory_fails_wrong_tool() -> None:
    score, assertions = await _evaluate_one(GOOD, {"expected_tools": ["debt_report"]})
    assert score == 0.0
    assert assertions["right_tool"].value is False


async def test_trajectory_fails_unapproved_write() -> None:
    executed = GOOD | {"tools_called": ["create_rule"], "write_executed": True}
    score, assertions = await _evaluate_one(executed, {"expected_tools": ["create_rule"]})
    assert score == 0.0
    assert assertions["write_safe"].value is False


async def test_trajectory_fails_ungrounded_number() -> None:
    fabricated = GOOD | {"answer": "You spent $999.99"}
    score, assertions = await _evaluate_one(fabricated, {"expected_tools": ["spending_report"]})
    assert score == 0.0
    assert assertions["grounded"].value is False


async def test_trajectory_requires_the_pause_on_write_tasks() -> None:
    unpaused = GOOD | {"tools_called": ["create_rule"], "paused": False}
    score, assertions = await _evaluate_one(
        unpaused, {"expected_tools": ["create_rule"], "must_pause": True}
    )
    assert score == 0.0
    assert assertions["pause_ok"].value is False


def test_chat_dataset_loads_with_trajectory_metadata() -> None:
    from pinch_backend.penny.evals import EVALS_ROOT

    dataset = Dataset.from_file(
        EVALS_ROOT / "chat" / "seed.yaml", custom_evaluator_types=[ChatTrajectory]
    )
    names = {case.name for case in dataset.cases}
    assert len(dataset.cases) >= 8
    assert any(case.metadata and case.metadata.get("must_pause") for case in dataset.cases)
    assert any(case.metadata and case.metadata.get("no_tools") for case in dataset.cases)
    assert "grounded-spending" in names


async def test_sandboxed_chat_task_records_the_trajectory(db, monkeypatch) -> None:
    """The machinery end-to-end under FunctionModel: sandbox provisioned
    through the model layer, tools through the public API, trajectory
    recorded — no live LLM."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_chat_model", "test")
    app = create_app(manage_database=False)
    sandbox = await provision_sandbox(app)

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart("Your checking balance is $12,500.00")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="list_accounts", args={}, tool_call_id="t1")]
        )

    task = chat_task("test", app, sandbox)
    with chat_agent.override(model=FunctionModel(scripted)):
        out = await task({"question": "what accounts do I have?"})

    assert out["tools_called"] == ["list_accounts"]
    assert out["paused"] is False
    assert out["write_executed"] is False
    assert "1250000" in out["tool_text"].replace(",", "")
    assert numbers_grounded(out["answer"], out["tool_text"])
