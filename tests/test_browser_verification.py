"""Verification access is explicit, scoped, pinned and JSON-round-trip safe."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ricky.browser.verification import (
    VerificationCeiling,
    VerificationMessage,
    VerificationQuery,
    compile_verification_ceiling,
    eligible_message,
)
from ricky.config import BrowserVerificationSettings, RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope


def configured() -> RickySettings:
    return RickySettings.model_validate(
        {
            "profiles": {"enabled": ["shared", "personal", "work"], "default": "personal"},
            "profile_configs": {
                "personal": {
                    "google": {
                        "accounts": {
                            "mail": {
                                "email": "owner@example.com",
                                "verification_aliases": ["alias@example.com"],
                            }
                        }
                    }
                },
                "work": {"google": {"accounts": {"mail": {"email": "work@example.com"}}}},
            },
            "browser": {
                "verification": {
                    "enabled": True,
                    "allow_background": True,
                    "gmail_accounts": ["personal/mail", "work/mail"],
                }
            },
        }
    )


def test_verification_ceiling_filters_profiles_and_pins_aliases():
    settings = configured()
    scope = ProfileScope.create("personal")
    ceiling = compile_verification_ceiling(settings, scope, background=True)
    assert ceiling is not None
    assert [item.account.qualified for item in ceiling.sources] == ["personal/mail"]
    assert ceiling.sources[0].recipients == ("alias@example.com", "owner@example.com")
    assert VerificationCeiling.model_validate_json(ceiling.model_dump_json()) == ceiling
    settings.browser.verification.allow_background = False
    assert compile_verification_ceiling(settings, scope, background=True) is None
    assert compile_verification_ceiling(settings, scope, background=False) is not None
    settings.browser.verification.enabled = False
    assert compile_verification_ceiling(settings, scope, background=False) is None


@pytest.mark.parametrize("value", ["mail", "personal/../mail", "personal/mail/extra"])
def test_verification_account_policy_requires_qualified_identity(value):
    with pytest.raises(ValueError):
        BrowserVerificationSettings(gmail_accounts=[value])


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/path",
        "https://user@example.com",
        "https://example.com:bad",
        "https://example.com:65536",
        "https://example.com:443",
        "https://example.com.",
        "https://example.com:0",
    ],
)
def test_verification_policy_requires_exact_https_origins(origin):
    with pytest.raises(ValidationError):
        BrowserVerificationSettings(allowed_origins=[origin])


def test_verification_candidates_require_connector_identity_recipient_and_freshness():
    ceiling = compile_verification_ceiling(
        configured(), ProfileScope.create("personal"), background=True
    )
    assert ceiling is not None
    now = datetime.now(UTC)
    query = VerificationQuery(
        challenge_id="browser_challenge_" + "a" * 32,
        source=ceiling.sources[0],
        recipient="owner@example.com",
        origin="https://merchant.example",
        issued_at=now,
        earliest_at=now - timedelta(seconds=120),
        expires_at=now + timedelta(seconds=60),
        limit=10,
    )
    message = VerificationMessage(
        account=ceiling.sources[0].account,
        message_id="message",
        received_at=now,
        sender="verify@merchant.example",
        recipients=("owner@example.com",),
        subject="Verify",
        text="Synthetic code",
    )
    assert eligible_message(message, query, now)
    for changes in (
        {"account": ProfileResourceRef(profile="work", name="mail")},
        {"recipients": ("other@example.com",)},
        {"received_at": now - timedelta(seconds=121)},
        {"received_at": now + timedelta(seconds=1)},
    ):
        assert not eligible_message(message.model_copy(update=changes), query, now)
    assert not eligible_message(message, query, query.expires_at)
    with pytest.raises(ValidationError):
        VerificationQuery.model_validate_json(
            query.model_dump_json().replace("owner@example.com", "other@example.com", 1)
        )
