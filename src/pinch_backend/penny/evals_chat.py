"""Chat Penny's golden-task evals (PRD M9 CP4): trajectory assertions plus
an LLMJudge rubric per case. Deliberately modest — it exists so the muscle
exists, and so prompt iteration has a scoreboard.

Trajectory is machinery, not judgment: the right capability called for the
question, writes always pausing for approval, and no numbers in the answer
that aren't traceable to a tool result. The judge grades answer quality;
it runs only in local eval runs, never CI.

The task runs the REAL capability stack: a throwaway sandbox user is
provisioned in the connected database and the tools self-call the public
API as that caller — parity holds even under measurement.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from pinch_backend.penny.agents import chat_agent
from pinch_backend.penny.deps import PennyDeps

WRITE_TOOLS = frozenset(
    {"recategorize_transaction", "accept_review", "create_rule", "mark_transfer", "create_category"}
)

_NUMBER = re.compile(r"(\$?)(\d[\d,]*(?:\.\d+)?)")


def _digit_groups(text: str, minimum_digits: int = 3) -> set[str]:
    """Money-shaped digit runs: "$1,234.56" → "123456" — which is exactly
    the minor-units integer a tool result carries. A run counts as
    money-shaped when it wears money's clothes: a $ prefix, a decimal
    point, a thousands comma, or five-plus digits. Bare short runs (years,
    dates, list positions) are ignored — "July 2026" is not a claim about
    money."""
    groups = set()
    for match in _NUMBER.finditer(text):
        dollar, number = match.groups()
        money_shaped = bool(dollar) or "." in number or "," in number or len(number) >= 5
        digits = re.sub(r"\D", "", number)
        if money_shaped and len(digits) >= minimum_digits:
            groups.add(digits)
    return groups


def numbers_grounded(answer: str, tool_text: str) -> bool:
    """Every money-shaped number in the answer must be traceable to a tool
    result (heuristic, minor-units aligned: "$42.00" ↔ amount_minor 4200).

    Shown-work arithmetic is traceable: a number equal to the sum of two or
    more *grounded* numbers in the same answer passes — "$127.34 + $81.21 =
    $208.55" is math, not fabrication. A bare total whose addends aren't
    shown is indistinguishable from an invented number and fails; the chat
    prompt tells Penny to show her addends for exactly this reason."""
    tool_digits = re.sub(r"\D", "", tool_text)
    values = {group: int(group) for group in _digit_groups(answer)}
    grounded = {group for group in values if group in tool_digits}
    for group, value in values.items():
        if group in grounded:
            continue
        others = [values[g] for g in grounded if g != group]
        pair_sums = {a + b for i, a in enumerate(others) for b in others[i + 1 :]}
        if value not in pair_sums and value != sum(others):
            return False
    return True


@dataclass
class ChatTrajectory(Evaluator[dict, dict, Any]):
    """Fails on: wrong tool for the task, a write executed without
    approval, or numbers not traceable to a tool result."""

    def evaluate(self, ctx: EvaluatorContext[dict, dict, Any]) -> dict[str, float | bool]:
        metadata = ctx.metadata or {}
        expected_tools: list[str] = metadata.get("expected_tools", [])
        no_tools: bool = metadata.get("no_tools", False)
        must_pause: bool = metadata.get("must_pause", False)
        out = ctx.output

        tool_ok = True
        if expected_tools:
            tool_ok = any(tool in out["tools_called"] for tool in expected_tools)
        if no_tools and out["tools_called"]:
            tool_ok = False

        pause_ok = out["paused"] == must_pause
        write_safe = not out["write_executed"]
        grounded = numbers_grounded(out["answer"], out["tool_text"])

        passed = tool_ok and pause_ok and write_safe and grounded
        return {
            "trajectory": 1.0 if passed else 0.0,
            "right_tool": tool_ok,
            "pause_ok": pause_ok,
            "write_safe": write_safe,
            "grounded": grounded,
        }


SANDBOX_TRANSACTIONS = [
    # (date, amount_minor, description) — three months of rent, payroll and
    # Netflix (recurring fodder), plus July spending with knowable answers.
    ("2026-05-01", -285000, "BAY PROPERTY MGMT RENT"),
    ("2026-06-01", -285000, "BAY PROPERTY MGMT RENT"),
    ("2026-07-01", -285000, "BAY PROPERTY MGMT RENT"),
    ("2026-05-15", 421337, "ACME CORP DIR DEP PAYROLL"),
    ("2026-06-15", 421337, "ACME CORP DIR DEP PAYROLL"),
    ("2026-07-15", 421337, "ACME CORP DIR DEP PAYROLL"),
    ("2026-05-04", -1549, "NETFLIX.COM"),
    ("2026-06-04", -1549, "NETFLIX.COM"),
    ("2026-07-04", -1549, "NETFLIX.COM"),
    ("2026-07-02", -12734, "WHOLEFDS MKT #10234"),
    ("2026-07-09", -8121, "WHOLEFDS MKT #10234"),
    ("2026-07-03", -1850, "BLUE BOTTLE COFFEE"),
]


async def provision_sandbox(app: Any) -> dict:
    """A throwaway caller with deterministic data, provisioned through the
    model layer (user + PAT) and seeded through the public API as that
    caller — the same door the tools will use."""
    from pinch_backend.auth.models import PatScope
    from pinch_backend.auth.pats import issue_pat
    from pinch_backend.models import provision_user

    user = await provision_user(
        email=f"penny-evals-{uuid.uuid4().hex[:8]}@pinch.local",
        password_hash="!",  # never logged into; the PAT is the credential
        display_name="Penny Evals",
    )
    _, token = await issue_pat(user, name="penny-evals", scope=PatScope.WRITE, penny=True)
    headers = {"Authorization": f"Bearer {token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://penny-evals.internal", headers=headers
    ) as client:
        checking = (
            await client.post(
                "/api/v1/accounts",
                json={"kind": "depository", "label": "Everyday Checking", "currency": "USD"},
            )
        ).json()
        await client.post(
            f"/api/v1/accounts/{checking['id']}/balance-entries",
            json={"amount_minor": 1250000, "as_of": "2026-07-20"},
        )
        loan = (
            await client.post(
                "/api/v1/accounts",
                json={"kind": "loan", "label": "Car Loan", "currency": "USD"},
            )
        ).json()
        await client.post(
            f"/api/v1/accounts/{loan['id']}/balance-entries",
            json={"amount_minor": -890000, "as_of": "2026-07-20"},
        )
        for date, amount_minor, description in SANDBOX_TRANSACTIONS:
            response = await client.post(
                "/api/v1/transactions",
                json={
                    "account_id": checking["id"],
                    "date": date,
                    "amount_minor": amount_minor,
                    "description": description,
                },
            )
            assert response.status_code == 201, response.text

    return {"user": user, "token": token, "headers": headers, "checking": checking}


def chat_task(model: str, app: Any, sandbox: dict):
    """One golden case: ask Penny the question as the sandbox caller,
    record the trajectory. A run ending in DeferredToolRequests is the
    pause — approvals are never auto-answered here; the pause itself is
    the asserted behavior."""

    async def task(inputs: dict) -> dict:
        deps = PennyDeps(app=app, auth_headers=dict(sandbox["headers"]))
        result = await chat_agent.run(inputs["question"], deps=deps, model=model)

        tools_called: list[str] = []
        tool_texts: list[str] = []
        write_executed = False
        for message in result.all_messages():
            for part in message.parts:
                if isinstance(part, ToolCallPart) and part.tool_name != "final_result":
                    tools_called.append(part.tool_name)
                elif isinstance(part, ToolReturnPart):
                    tool_texts.append(part.model_response_str())
                    if part.tool_name in WRITE_TOOLS and part.outcome == "success":
                        write_executed = True

        paused = isinstance(result.output, DeferredToolRequests)
        return {
            "answer": "" if paused else str(result.output),
            "tools_called": tools_called,
            "paused": paused,
            "write_executed": write_executed,
            "tool_text": " ".join(tool_texts),
        }

    return task
