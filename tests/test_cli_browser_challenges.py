"""CLI responses share the browser lifecycle and never echo an entered OTP."""

from io import StringIO
from typing import Any, cast

import pytest
from pydantic import SecretStr
from rich.console import Console

from ricky.browser.challenges import LiveBrowserChallenge
from ricky.interfaces.cli.input import CliInputSession
from ricky.interfaces.cli.render import CliRenderer
from test_browser_challenges import Journal, record


@pytest.mark.parametrize("answer", ["123456", ""])
async def test_cli_challenge_hidden_input_and_blank_cancellation(answer):
    output = StringIO()
    console = Console(file=output, force_terminal=False)
    inputs = CliInputSession(console, stdin=StringIO(), interactive=False)

    class Prompt:
        async def prompt_async(self, prompt, **kwargs):
            assert kwargs["is_password"] is True
            assert "https://merchant.example" in prompt
            return answer

    inputs._secret_prompt_session = cast(Any, Prompt())
    initial = record()
    journal = Journal(initial)
    owner = LiveBrowserChallenge(initial, writer=journal.write)
    await CliRenderer(console=console, input_session=inputs).request_browser_challenge(owner)
    assert owner.record.source is not None
    assert owner.record.source.principal_id == "local-cli"
    if answer:
        assert (await owner.wait()).code == SecretStr(answer)
        assert answer not in output.getvalue()
        assert answer not in owner.record.model_dump_json()
    else:
        assert owner.record.state == "cancelled"


@pytest.mark.parametrize("answer", ["done", ""])
async def test_cli_manual_response_does_not_claim_resolution(answer, monkeypatch):
    initial = record().model_copy(update={"kind": "manual"})
    journal = Journal(initial)
    owner = LiveBrowserChallenge(initial, writer=journal.write)
    renderer = CliRenderer(console=Console(file=StringIO()))

    async def read_line(_prompt):
        return answer

    monkeypatch.setattr(renderer, "read_line", read_line)
    await renderer.request_browser_challenge(owner)
    if answer:
        assert owner.record.state == "responded"
        assert (await owner.wait()).code is None
    else:
        assert owner.record.state == "cancelled"
