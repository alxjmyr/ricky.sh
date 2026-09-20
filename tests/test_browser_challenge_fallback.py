"""Email failures fall back to a correlated reply without submitting an effect."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from browser_challenge_support import configured
from browser_support import FakeBrowserBackend, FakeBrowserPage, FakeBrowserSession, fake_executable
from ricky.browser.backend import BackendTargetDescriptor
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenges import ChallengeResponse, ChallengeSource, LiveBrowserChallenge
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.browser.types import BrowserActionTarget
from ricky.browser.verification import (
    VerificationMessage,
    VerificationQuery,
    VerificationUnavailable,
    compile_verification_ceiling,
)
from ricky.browser.verification_resolver import BrowserVerificationResolver, VerificationAnswer
from ricky.browser.verification_store import VerificationClaimStore


@pytest.mark.parametrize("failure", ["unavailable", "uninterpretable"])
async def test_email_fallback_keeps_live_binding_and_requires_manual_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    settings = configured()
    settings.user_data_dir = str(tmp_path / "user")
    settings.project_data_dir = str(tmp_path / "project")
    settings.browser.enabled = True
    scope = settings.resolve_profile_scope()
    origin = "https://merchant.example"
    page = FakeBrowserPage(
        url=origin + "/verify",
        snapshot='- textbox "Verification code" [ref=e1]',
        targets=(
            BackendTargetDescriptor(
                ref="e1",
                role="textbox",
                name="Verification code",
                control_kind="text",
                editable=True,
                protected=True,
                protected_kind="one_time_code",
                frame_origin=origin,
            ),
        ),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))

    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kwargs: DestinationPolicy(resolver=resolve, **kwargs),
    )
    service = BrowserService(
        settings,
        scope=scope,
        backend=backend,
        executable_path=fake_executable(tmp_path),
    )
    ceiling = compile_verification_ceiling(settings, scope, background=True)
    assert ceiling is not None
    account = ceiling.sources[0].account
    email = VerificationMessage(
        account=account,
        message_id="fixture-message",
        received_at=datetime.now(UTC),
        sender="verify@merchant.example",
        recipients=("owner@example.com",),
        subject="Verify your purchase",
        text="Use your mobile authenticator to continue.",
    )

    class Reader:
        async def read(self, query: VerificationQuery) -> tuple[VerificationMessage, ...]:
            assert query.source.account == account
            assert query.recipient == "owner@example.com"
            if failure == "unavailable":
                raise VerificationUnavailable("The authorized Gmail account needs reconnection.")
            return (email,)

    async def validate() -> None:
        pass

    claims = VerificationClaimStore(settings)
    resolver = BrowserVerificationResolver(ceiling, Reader(), claims, validate)
    prompted = []

    async def respond(owner: LiveBrowserChallenge) -> None:
        prompted.append(owner.record.id)
        assert owner.assistance_reason == (
            "The authorized Gmail account needs reconnection."
            if failure == "unavailable"
            else "The verification email did not contain an identifiable code."
        )
        assert page.actions == [] and page.protected_fills == []
        source = ChallengeSource(
            principal_id="owner",
            conversation_id="conversation",
            prompt_message_id="prompt",
        )
        await owner.bind_source(source)
        await owner.respond(ChallengeResponse(code=SecretStr("123456")), source=source)

    try:
        opened = await service.open_session()
        snapshot = await service.snapshot(opened.session_id, page_id=None)
        target = BrowserActionTarget(
            session_id=opened.session_id,
            page_id=snapshot.page.page_id,
            snapshot_id=snapshot.snapshot_id,
            ref="e1",
        )
        record, resolution = await service.request_challenge(
            target,
            instruction="Enter the emailed code.",
            purpose="transaction",
            responder=respond,
            resolver=resolver,
            recipient="owner@example.com",
        )
        if failure == "uninterpretable":
            assert resolution is not None and resolution.state == "interpretation"
            assert prompted == [] and record.state == "waiting_for_user"
            record = await service.answer_verification(
                target,
                VerificationAnswer(challenge_id=record.id, message_id=email.message_id),
                responder=respond,
            )
        assert prompted == [record.id]
        assert record.state == "responded"
        assert record.binding.session_id == opened.session_id
        assert record.binding.page_id == snapshot.page.page_id
        assert page.actions == [] and page.protected_fills == []
        assert not await claims.claimed(account, email.message_id, scope=scope)
        assert "123456" not in record.model_dump_json()
        await service.cancel_challenge(target, record.id)
        records = await BrowserChallengeStore(settings).list(scope=scope)
        assert len(records) == 1 and records[0].state == "cancelled"
    finally:
        await service.aclose()
    assert backend.closed
    assert not Path(settings.project_data_dir).exists()
