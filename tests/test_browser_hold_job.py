"""Named read-oriented workers can verify without ordinary mutation authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from browser_support import FakeBrowserBackend, FakeBrowserPage, FakeBrowserSession, fake_executable
from ricky.browser.backend import (
    BackendBoundingBox,
    BackendTargetDescriptor,
    BackendViewport,
    BackendVisualCandidate,
    BackendVisualSnapshot,
)
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.config import RickySettings
from ricky.jobs.runner import JobRunner
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, ToolCallPart
from ricky.project_scope import ProjectScope
from ricky.runtime import build_session_runtime


class VerificationWorker:
    name = "openrouter"

    def __init__(self) -> None:
        self.step = 0

    async def aclose(self) -> None:
        pass

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.step += 1
        system_text = "\n".join(
            part.text
            for message in request.messages
            if message.role == "system"
            for part in message.content
            if part.kind == "text"
        )
        assert "authorized bounded verification maintenance" in system_text
        assert "including in read_only mode" in system_text
        results = [p for m in request.messages for p in m.content if p.kind == "tool_result"]
        assert not any(part.is_error for part in results), [part.content for part in results]
        text = "\n".join(part.content for part in results)

        def ref(prefix: str) -> str:
            match = re.search(prefix + r"_[0-9a-f]{32}", text)
            assert match is not None, text
            return match.group()

        args: dict[str, Any]
        if self.step == 1:
            name, args = "browser_session_open", {"headless": True}
        elif self.step == 2:
            name, args = (
                "browser_navigate",
                {"session_id": ref("browser_session"), "url": "https://example.com/challenge"},
            )
        elif self.step in {3, 5, 7}:
            if self.step == 7:
                assert '"state":"released"' in results[-1].content
                assert "Next call browser_visual_snapshot" in results[-1].content
                assert "not a verification verdict" in results[-1].content
            name, args = "browser_visual_snapshot", {"session_id": ref("browser_session")}
        elif self.step == 4:
            latest = results[-1].content
            untrusted = json.loads(
                latest.split("BEGIN_UNTRUSTED_BROWSER_CONTENT\n")[1].split("\nEND_UNTRUSTED")[0]
            )
            candidate = untrusted["candidates"][0]
            name, args = (
                "browser_hold_start",
                {
                    "target": {
                        "session_id": ref("browser_session"),
                        "page_id": ref("browser_page"),
                        "snapshot_id": ref("browser_snapshot"),
                        "ref": candidate["target"]["ref"],
                    }
                },
            )
        elif self.step == 6:
            assert "Observation-only capture during a verification hold" in results[-1].content
            assert "not evidence of clearance" in results[-1].content
            name, args = "browser_hold_release", {"hold_id": ref("browser_hold")}
        else:
            assert '"state":"released"' in text
            assert "Observation-only capture" not in results[-1].content
            yield MessageDone(message=Message.text("assistant", "Verification input released."))
            return
        yield MessageDone(
            message=Message(
                role="assistant",
                content=[
                    ToolCallPart(
                        id=f"call_{self.step}",
                        name=name,
                        args=args,
                    )
                ],
            ),
            stop_reason="tool_calls",
        )


async def test_named_read_worker_holds_observes_releases_and_records_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "browser": {
                "enabled": True,
                "background": {
                    "enabled": True,
                    "read_enabled": True,
                    "allow_ephemeral": True,
                    "allow_public_https_research": True,
                },
            },
            "profile_configs": {
                "personal": {"browser": {"screenshot_allowed_providers": ["openrouter"]}}
            },
        }
    )
    root = tmp_path / "user" / "profiles" / "personal" / "jobs" / "verify"
    root.mkdir(parents=True)
    (root / "job.toml").write_text("""version = 3
name = "verify"
description = "Inspect a site and clear supported human verification."
provider = "openrouter"
model = "test-model"
goal = "Inspect the challenge and release after observing it."
[budget]
iterations = 8
effect_calls = 0
[tools]
allow = [
    "browser_session_open", "browser_navigate", "browser_visual_snapshot",
    "browser_hold_start", "browser_hold_status", "browser_hold_release",
]
[browser]
allow_public_https_research = true
allow_masked_visual_observations = true
""")
    page = FakeBrowserPage()
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="button",
        name="Human verification hold",
        frame_origin="https://example.com",
        restricted_interaction="captcha",
    )
    page.coordinate_target = descriptor
    output = BytesIO()
    Image.new("RGB", (100, 50), (230, 230, 230)).save(output, format="PNG")
    png = output.getvalue()
    page.visual_capture = BackendVisualSnapshot(
        png=png,
        masked_base_sha256=hashlib.sha256(png).hexdigest(),
        viewport=BackendViewport(100, 50, 0, 0, 1),
        candidates=(BackendVisualCandidate(descriptor, BackendBoundingBox(10, 5, 20, 10)),),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))

    async def resolve(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kw: DestinationPolicy(resolver=resolve, **kw),
    )

    async def browser_factory(runtime_settings, **kwargs):
        return BrowserService(
            runtime_settings, backend=backend, executable_path=fake_executable(tmp_path), **kwargs
        )

    @asynccontextmanager
    async def runtime_factory(*args, **kwargs):
        async with build_session_runtime(
            *args, background_browser_factory=browser_factory, **kwargs
        ) as runtime:
            yield runtime

    monkeypatch.setattr("ricky.jobs.runner.build_session_runtime", runtime_factory)
    worker = VerificationWorker()
    runner = JobRunner(settings, project_scope=ProjectScope.disabled())
    scope = settings.resolve_profile_scope()
    run = await runner.run("personal/verify", profile_scope=scope, provider=worker)
    assert run.outcome == "succeeded", run
    assert worker.step == 8
    assert len(page.holds) == 1 and page.holds[0].release_calls == 1
    assert page.closed
    assert run.effect_calls == 0
    assert not settings.browser.background.interaction_enabled
    assert not settings.browser.background.commit_enabled
    actions = await runner.store.actions_for_run(run.id, scope=scope)
    assert {action.operation for action in actions} == {
        "browser_hold_start",
        "browser_hold_release",
    }
    assert all(action.status == "performed" for action in actions)
    assert not Path(settings.project_data_dir).exists()
