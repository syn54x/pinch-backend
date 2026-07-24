"""M9 CP3: the evals harness machinery (issue #57).

CI asserts the machinery — dataset hygiene, asymmetric scoring, the
exporter's user-decisions-only charter. Quality is evals, not tests: no
live model runs here, ever.
"""

import pytest
from pydantic_evals import Case, Dataset

from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    LedgerMember,
    User,
)
from pinch_backend.penny.evals import (
    CategoryScore,
    categorization_task,
    default_taxonomy_paths,
    export_correction_log,
    load_dataset,
    mapping_task,
)

PASSWORD = "correct horse battery staple"


def test_seed_dataset_loads_and_stays_inside_the_default_taxonomy() -> None:
    """Dataset hygiene: every expected path must be a real default-taxonomy
    path (or null) — a typo here would teach the wrong lesson forever."""
    dataset = load_dataset("categorization")
    valid = set(default_taxonomy_paths())
    assert len(dataset.cases) >= 20
    abstains = 0
    for case in dataset.cases:
        if case.expected_output is None:
            abstains += 1
        else:
            assert case.expected_output in valid, case.name
    assert abstains >= 3  # calibration is measured, so abstain cases exist


async def test_asymmetric_scoring_orders_exact_ancestor_abstain_wrong() -> None:
    """abstain > wrong, always; exact > ancestor > both."""
    answers = {
        "exact": "Food & Drink > Coffee",
        "ancestor": "Food & Drink",
        "abstain": None,
        "wrong": "Travel > Flights",
    }
    dataset = Dataset(
        name="scoring-probe",
        cases=[
            Case(name=name, inputs={"answer": name}, expected_output="Food & Drink > Coffee")
            for name in answers
        ],
        evaluators=[CategoryScore()],
    )

    async def task(inputs: dict) -> str | None:
        return answers[inputs["answer"]]

    report = await dataset.evaluate(task, progress=False)
    scores = {case.name: case.scores["score"].value for case in report.cases}
    assert scores["exact"] == 1.0
    assert scores["exact"] > scores["ancestor"] > scores["abstain"] > scores["wrong"]
    assert scores["wrong"] == 0.0


async def test_expected_abstain_scores_only_an_abstain() -> None:
    dataset = Dataset(
        name="abstain-probe",
        cases=[Case(name="ambiguous", inputs={}, expected_output=None)],
        evaluators=[CategoryScore()],
    )

    async def confident(inputs: dict) -> str | None:
        return "Food & Drink > Coffee"

    async def honest(inputs: dict) -> str | None:
        return None

    confident_report = await dataset.evaluate(confident, progress=False)
    honest_report = await dataset.evaluate(honest, progress=False)
    assert confident_report.cases[0].scores["score"].value == 0.0
    assert honest_report.cases[0].scores["score"].value == 1.0


CATEGORIZATION_INPUTS = {
    "payee": "Blue Bottle Coffee",
    "description": "BLUE BOTTLE COFFEE SF",
    "amount_minor": -575,
    "currency": "USD",
    "date": "2026-07-01",
    "account_label": "Checking",
    "account_kind": "depository",
}
BROKEN_ROUTE = "nonexistent-provider:nonexistent-model"


async def test_categorization_records_the_provider_errors_it_degrades_over() -> None:
    """A broken route degrades to abstain — which *scores*, and reads
    exactly like honest calibration. Degradation stays (it is the
    production shape); the swallowed cause becomes visible."""
    errors: list[str] = []
    task = categorization_task(BROKEN_ROUTE, errors)

    assert await task(CATEGORIZATION_INPUTS) is None
    assert len(errors) == 1
    assert BROKEN_ROUTE in errors[0]


async def test_mapping_records_the_provider_errors_it_degrades_over() -> None:
    errors: list[str] = []
    task = mapping_task(BROKEN_ROUTE, errors)

    assert await task({"csv": "date,amount\n2026-07-01,-5.75\n"}) is None
    assert len(errors) == 1


async def test_the_error_sink_is_optional_so_production_shape_is_unchanged() -> None:
    """No sink, no bookkeeping: the task still abstains silently."""
    assert await categorization_task(BROKEN_ROUTE)(CATEGORIZATION_INPUTS) is None


async def _seed_log_entry(ledger: Ledger, **overrides) -> CorrectionLogEntry:
    defaults = dict(
        ledger=ledger,
        transaction_id=__import__("uuid").uuid7(),
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        input_payee="blue bottle coffee",
        input_description_raw="BLUE BOTTLE COFFEE OAK-3",
        input_amount_minor=-1850,
        input_currency="USD",
    )
    return await CorrectionLogEntry.create(**defaults | overrides)


@pytest.fixture
async def seeded_ledger(client):
    async def csrf():
        if "csrftoken" not in client.cookies:
            await client.get("/health")
        return {"x-csrftoken": client.cookies["csrftoken"]}

    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": "taylor@example.com", "password": PASSWORD, "display_name": "T"},
        headers=await csrf(),
    )
    assert response.status_code == 201
    user = await User.where(lambda u: u.email == "taylor@example.com").first()
    membership = await LedgerMember.where(lambda m: m.user_id == user.id).first()
    ledger = await Ledger.get(membership.ledger_id)
    categories = (await client.get("/api/v1/categories", params={"limit": 100})).json()["items"]
    coffee = next(c for c in categories if c["name"] == "Coffee")
    return {"ledger": ledger, "coffee": coffee}


async def test_export_takes_user_decisions_only(seeded_ledger, tmp_path) -> None:
    """The charter, mechanized: auto-filed and voided entries never become
    eval cases; user decisions come out with full taxonomy paths."""
    ledger, coffee = seeded_ledger["ledger"], seeded_ledger["coffee"]

    kept = await _seed_log_entry(
        ledger, decision_category_id=coffee["id"], decision_category_name="Coffee"
    )
    await _seed_log_entry(
        ledger,
        actor=CorrectionActor.AUTO,
        input_payee="auto filed vendor",
        decision_category_id=coffee["id"],
        decision_category_name="Coffee",
    )
    voided = await _seed_log_entry(
        ledger, input_payee="voided vendor", decision_category_name="Coffee"
    )
    await _seed_log_entry(ledger, kind=CorrectionKind.VOID, voids=voided.id, input_payee=None)

    out = tmp_path / "export.yaml"
    count = await export_correction_log(out)
    assert count == 1

    exported = Dataset.from_file(out, custom_evaluator_types=[CategoryScore])
    (case,) = exported.cases
    assert case.name == f"log-{kept.id}"
    assert case.inputs["payee"] == "blue bottle coffee"
    assert case.expected_output == "Food & Drink > Coffee"
    assert "Food & Drink > Coffee" in case.inputs["taxonomy"]


async def test_export_of_an_uncategorized_decision_is_an_abstain_case(
    seeded_ledger, tmp_path
) -> None:
    """Accepting a transaction as uncategorized is a real judgment — the
    honest-abstain half of the dataset grows from it."""
    await _seed_log_entry(seeded_ledger["ledger"], input_payee="mystery vendor")
    out = tmp_path / "export.yaml"
    assert await export_correction_log(out) == 1
    exported = Dataset.from_file(out, custom_evaluator_types=[CategoryScore])
    assert exported.cases[0].expected_output is None


async def test_mapping_dataset_holds_only_shapes_the_heuristic_abstains_on() -> None:
    """Hygiene: a case the heuristic already maps measures nothing — the
    dataset exists to score Penny's layer, so every case must be a
    heuristic abstention."""
    from pinch_backend.imports.inference import HeuristicInferrer

    dataset = load_dataset("mapping")
    assert len(dataset.cases) >= 5
    assert any(case.expected_output is None for case in dataset.cases)
    heuristic = HeuristicInferrer()
    for case in dataset.cases:
        suggestion = await heuristic.suggest(case.inputs["csv"])
        assert suggestion is None, f"{case.name}: heuristic maps this; the case is inert"


async def test_mapping_score_orders_exact_partial_abstain_wrong() -> None:
    from pinch_backend.penny.evals import MappingScore

    expected = {
        "delimiter": ";",
        "has_header": True,
        "date_column": 0,
        "date_format": "%b-%d-%y",
        "amount_column": 1,
        "debit_column": None,
        "credit_column": None,
        "sign": "negative_out",
        "description_columns": [2],
    }
    answers: dict[str, dict | None] = {
        "exact": expected,
        "partial": expected | {"amount_column": 2, "description_columns": [1]},
        "abstain": None,
        "wrong": {
            "delimiter": ",",
            "has_header": False,
            "date_column": 3,
            "date_format": "%Y",
            "amount_column": 0,
            "debit_column": None,
            "credit_column": None,
            "sign": "positive_out",
            "description_columns": [],
        },
    }
    dataset = Dataset(
        name="mapping-probe",
        cases=[
            Case(name=name, inputs={"answer": name}, expected_output=expected) for name in answers
        ],
        evaluators=[MappingScore()],
    )

    async def task(inputs: dict) -> dict | None:
        return answers[inputs["answer"]]

    report = await dataset.evaluate(task, progress=False)
    scores = {case.name: case.scores["score"].value for case in report.cases}
    assert scores["exact"] == 1.0
    assert scores["exact"] > scores["partial"] > scores["abstain"] > scores["wrong"]
    assert scores["wrong"] == 0.0


async def test_mapping_score_on_a_hopeless_file() -> None:
    from pinch_backend.penny.evals import MappingScore

    dataset = Dataset(
        name="hopeless-probe",
        cases=[Case(name="hopeless", inputs={}, expected_output=None)],
        evaluators=[MappingScore()],
    )

    async def honest(inputs: dict) -> dict | None:
        return None

    async def confident(inputs: dict) -> dict | None:
        return {"delimiter": ",", "has_header": True, "date_column": 0}

    honest_report = await dataset.evaluate(honest, progress=False)
    confident_report = await dataset.evaluate(confident, progress=False)
    assert honest_report.cases[0].scores["score"].value == 1.0
    assert confident_report.cases[0].scores["score"].value == 0.0
