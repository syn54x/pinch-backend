"""M9 CP1: Penny availability (issue #55).

Keyless degradation is a first-class, tested state (PRD M9): an agent whose
model knob is unset — or whose knob names a provider with no key — is
disabled with a reason, and nothing else in the instance is touched. The
conftest blanks every AI knob and key, so keyless is the suite's baseline.
"""

STATUS = "/api/v1/penny/status"
PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com"):
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def test_status_requires_a_credential(client) -> None:
    """Instance AI configuration is not anonymous information."""
    assert (await client.get(STATUS)).status_code == 401


async def test_keyless_instance_reports_penny_unavailable_with_reason(client) -> None:
    await _signup(client)
    body = (await client.get(STATUS)).json()
    assert body["available"] is False
    assert "PINCH_AI_CHAT_MODEL" in body["reason"]
    assert set(body["agents"]) == {"chat", "categorization", "mapping"}
    assert all(agent["available"] is False for agent in body["agents"].values())


async def test_configured_chat_model_reports_available(client, monkeypatch) -> None:
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_chat_model", "test")
    await _signup(client)
    body = (await client.get(STATUS)).json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["agents"]["chat"] == {"available": True, "reason": None}
    assert body["agents"]["categorization"]["available"] is False


async def test_model_with_missing_provider_key_is_unavailable_with_reason(
    client, monkeypatch
) -> None:
    """A knob naming a real provider whose key is absent is a misconfigured
    agent, not a 500: the reason surfaces the provider's own complaint."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_chat_model", "anthropic:claude-haiku-4-5")
    await _signup(client)
    body = (await client.get(STATUS)).json()
    assert body["available"] is False
    assert "ANTHROPIC_API_KEY" in body["reason"]
