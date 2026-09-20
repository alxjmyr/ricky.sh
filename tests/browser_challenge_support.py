"""Challenge records, journals, and verification settings for focused browser tests."""

from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from ricky.browser.challenges import (
    BrowserChallenge,
    ChallengeBinding,
    ChallengeResponse,
    ChallengeSource,
    LiveBrowserChallenge,
)
from ricky.config import RickySettings
from ricky.profiles import ProfileScope


def record(**updates) -> BrowserChallenge:
    now = datetime.now(UTC)
    return BrowserChallenge(
        id="browser_challenge_" + "a" * 32,
        binding=ChallengeBinding(
            owner_id="test-owner",
            profile_scope=ProfileScope.create("personal"),
            session_id="browser_session_" + "b" * 32,
            page_id="browser_page_" + "c" * 32,
            page_generation=2,
            top_level_origin="https://merchant.example",
            frame_origin="https://verify.example",
            occurrence_digest="d" * 64,
            purpose="authentication",
        ),
        kind="otp",
        instruction="Reply with the verification code.",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        **updates,
    )


SOURCE = ChallengeSource(
    principal_id="telegram:personal/bot:sender",
    conversation_id="conversation",
    prompt_message_id="100",
)


RESPONSE = ChallengeResponse(code=SecretStr("synthetic-otp"))


class Journal:
    def __init__(self, initial: BrowserChallenge) -> None:
        self.records = [initial]

    async def write(self, updated: BrowserChallenge, expected: int) -> None:
        assert self.records[-1].revision == expected
        self.records.append(BrowserChallenge.model_validate_json(updated.model_dump_json()))


async def live() -> tuple[LiveBrowserChallenge, Journal]:
    initial = record()
    journal = Journal(initial)
    owner = LiveBrowserChallenge(initial, writer=journal.write)
    await owner.bind_source(SOURCE)
    return owner, journal


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
