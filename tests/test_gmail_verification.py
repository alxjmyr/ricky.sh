"""The challenge adapter authenticates identity and only performs bounded reads."""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ricky.browser.verification import (
    VerificationQuery,
    VerificationUnavailable,
    compile_verification_ceiling,
)
from ricky.config import GoogleAccountSettings
from ricky.profiles import ProfileScope
from ricky.tools.integrations.gmail.client import GmailClient
from ricky.tools.integrations.gmail.verification import GmailVerificationReader
from test_browser_verification import configured
from test_gmail_client import FakeAuth


@pytest.mark.parametrize(
    "scenario", ["valid", "alias", "foreign", "stale", "header_only", "wrong_identity"]
)
async def test_gmail_verification_uses_server_date_account_and_recipient(scenario):
    now = datetime.now(UTC)
    ceiling = compile_verification_ceiling(
        configured(), ProfileScope.create("personal"), background=True
    )
    assert ceiling is not None
    query = VerificationQuery(
        challenge_id="browser_challenge_" + "a" * 32,
        source=ceiling.sources[0],
        recipient="alias@example.com" if scenario == "alias" else "owner@example.com",
        origin="https://merchant.example",
        issued_at=now,
        earliest_at=now - timedelta(seconds=120),
        expires_at=now + timedelta(minutes=1),
        limit=2,
    )

    class Auth(FakeAuth):
        def validate_account(self, account):
            assert account == "personal/mail"
            return GoogleAccountSettings(email="owner@example.com")

    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/profile"):
            return httpx.Response(
                200,
                json={
                    "emailAddress": "other@example.com"
                    if scenario == "wrong_identity"
                    else "owner@example.com"
                },
            )
        if request.url.path.endswith("/messages"):
            assert request.url.params["maxResults"] == "2"
            assert request.url.params["q"] == f"after:{int(query.earliest_at.timestamp())}"
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": "message_1"}, {"id": "message_1"}],
                    "nextPageToken": "not-followed",
                },
            )
        payload = {
            "id": "message_1",
            "internalDate": str(
                int((now - timedelta(seconds=121 if scenario == "stale" else 0)).timestamp() * 1000)
            ),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "verify@merchant.example"},
                    {
                        "name": "To",
                        "value": "someone@example.com"
                        if scenario == "foreign"
                        else query.recipient,
                    },
                    {"name": "Date", "value": now.strftime("%a, %d %b %Y %H:%M:%S +0000")},
                ],
                "body": {"data": base64.urlsafe_b64encode(b"Verification code: 123456").decode()},
            },
        }
        if scenario == "header_only":
            payload.pop("internalDate")
        return httpx.Response(200, json=payload)

    client = GmailClient(
        auth=Auth(),
        base_url="https://gmail.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handle),
    )
    try:
        reader = GmailVerificationReader(client)
        if scenario == "wrong_identity":
            with pytest.raises(VerificationUnavailable, match="unavailable"):
                await reader.read(query)
            assert len(calls) == 1
        else:
            result = await reader.read(query)
            assert len(result) == (1 if scenario in {"valid", "alias"} else 0)
            assert len(calls) == 3
    finally:
        await client.aclose()
