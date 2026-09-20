"""Shared browser checkout fixtures and scripted providers."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from gateway_conversation_support import (
    AdHocProvider,
    _answer,
    _settings,
    _tool,
)
from ricky.config import RickySettings
from ricky.llm import CompletionRequest, StreamEvent

ORIGIN = "https://checkout.example"


def checkout_settings(tmp_path: Path) -> RickySettings:
    raw = _settings(tmp_path).model_dump(mode="python")
    raw["project_data_dir"] = str(tmp_path / "project")
    raw["browser"] = {
        "enabled": True,
        "background": {
            "enabled": True,
            "read_enabled": True,
            "interaction_enabled": True,
            "commit_enabled": True,
            "budget": {"transaction_commits": 1},
        },
    }
    raw["profile_configs"] = {
        "personal": {
            "browser": {
                "resources": {
                    "checkout": {
                        "kind": "persistent",
                        "headless": True,
                        "description": "Synthetic checkout",
                    },
                }
            }
        }
    }
    raw["authority"] = {
        "enabled": True,
        "allowed_principals": ["telegram:personal/bot:100"],
        "max_effect_calls": 20,
        "capabilities": {
            "browser_interact": {
                "enabled": True,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20,
            },
            "browser_commit": {
                "enabled": True,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20,
                "max_financial_limit_minor": 3000,
                "currency": "USD",
            },
        },
    }
    raw["agents"] = {
        "ad_hoc_background": {
            "confirmation_required_capabilities": [],
            "guardrail_required_capabilities": [],
            "execution": {"effect_calls": 20, "iterations": 30},
        }
    }
    return RickySettings.model_validate(raw)


class CheckoutDelegator(AdHocProvider):
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        if self.step != 1:
            async for event in super().stream(request):
                yield event
            return
        self.requests.append(request)
        self.step += 1
        results = "\n".join(
            p.content for m in request.messages for p in m.content if p.kind == "tool_result"
        )
        task = re.search(r"task_[0-9a-f]{32}", results)
        assert task is not None
        tools = {
            "builtin.browser.read": "browser_navigate,browser_snapshot",
            "builtin.browser.interact": "browser_session_open_resource,browser_click,browser_fill",
            "builtin.browser.commit": "browser_commit",
        }
        yield _tool(
            "delegate",
            "delegate_task",
            {
                "action": "start",
                "task_id": task.group(),
                "expected_task_revision": 1,
                "goal": "Check balance. If below $10 buy $20 credits with fees within $30. "
                "Report starting and ending balance.",
                "requested_capabilities": list(tools),
                "guardrails": [
                    {
                        "capability_id": cap,
                        "fields": [
                            {"field": "mode", "value": "transaction"},
                            {"field": "allowed_tools", "value": names},
                            {"field": "resources", "value": "personal/checkout"},
                            {
                                "field": "authenticated_origins",
                                "value": f"personal/checkout#{ORIGIN}",
                            },
                        ],
                    }
                    for cap, names in tools.items()
                ],
            },
        )


class CheckoutWorker(AdHocProvider):
    """Script the task, using only actual model-visible observations for target ids."""

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self.step += 1
        results = [
            p.content for m in request.messages for p in m.content if p.kind == "tool_result"
        ]
        if self.step == 1:
            yield _tool("open", "browser_session_open_resource", {"resource": "personal/checkout"})
            return
        session = re.search(r"browser_session_[0-9a-f]{32}", "\n".join(results))
        assert session is not None
        if self.step == 2:
            yield _tool(
                "navigate", "browser_navigate", {"session_id": session.group(), "url": ORIGIN}
            )
        elif self.step in {3, 5, 7, 9}:
            yield _tool(
                f"snapshot-{self.step}", "browser_snapshot", {"session_id": session.group()}
            )
        elif self.step in {4, 6, 8}:
            latest = results[-1]
            name = {4: "Open checkout", 6: "Credit amount (USD)", 8: "Purchase credits"}[self.step]
            controls = [
                json.loads(line)
                for line in latest.splitlines()
                if line.startswith("{") and '"control_kind"' in line
            ]
            control = next((c for c in controls if c.get("name") == name), None)
            assert control is not None, f"Missing {name!r} in checkout observation:\n{latest}"
            snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
            page = re.search(r"browser_page_[0-9a-f]{32}", latest)
            assert snapshot is not None and page is not None
            args: dict[str, object] = {
                "target": {
                    "session_id": session.group(),
                    "page_id": page.group(),
                    "snapshot_id": snapshot.group(),
                    "ref": control["ref"],
                }
            }
            if self.step == 6:
                args["value"] = "20"
            if self.step == 8:
                assert "21.60" in latest
                args["envelope"] = {
                    "kind": "financial",
                    "intent": "Buy $20 credits",
                    "payee": "Fixture merchant",
                    "total": {"amount": "21.60", "currency": "USD"},
                    "components": [
                        {"label": "Credits", "amount": {"amount": "20.00", "currency": "USD"}}
                    ],
                    "fees": [
                        {"label": "Service fee", "amount": {"amount": "1.60", "currency": "USD"}}
                    ],
                    "timing": "one_time",
                    "source": {"kind": "site", "label": "Saved account"},
                    "consequences": ["One-time charge of $21.60"],
                    "expected_result": "Credit balance becomes $26.42",
                }
            yield _tool(
                f"act-{self.step}",
                {4: "browser_click", 6: "browser_fill", 8: "browser_commit"}[self.step],
                args,
            )
        elif self.step == 10:
            assert "26.42" in results[-1]
            yield _tool(
                "outcome",
                "report_task_outcome",
                {
                    "status": "completed",
                    "summary": "Credit purchase completed.",
                    "evidence": ["The resulting page shows credit balance $26.42."],
                },
            )
        else:
            yield _answer("Starting balance: $6.42. Charged: $21.60. Ending balance: $26.42.")
