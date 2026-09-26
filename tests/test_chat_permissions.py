"""Chat-local prompt bypass, reversible without broadening saved authority."""

from unittest.mock import AsyncMock

import pytest

from ricky.agent.events import PermissionRequestedEvent
from ricky.agent.workflow import ApprovalRequest, ApprovalResponse
from ricky.interfaces.cli.chat_permissions import ChatPermissions
from ricky.permissions import PermissionResponse
from ricky.profiles import ProfileResourceRef
from ricky.protected_values import DestinationApprovalRequest, DestinationApprovalResponse


@pytest.mark.asyncio
async def test_chat_permissions_toggle_and_instance_isolation() -> None:
    permission = AsyncMock(return_value=PermissionResponse(decision="deny"))
    approval = AsyncMock(return_value=ApprovalResponse(approved=False))
    destination = AsyncMock(return_value=DestinationApprovalResponse(decision="deny"))
    normal = ChatPermissions(
        permission_responder=permission,
        approval_responder=approval,
        destination_responder=destination,
    )
    enabled = ChatPermissions(
        permission_responder=permission,
        approval_responder=approval,
        destination_responder=destination,
        enabled=True,
    )
    request = PermissionRequestedEvent(
        turn_id="turn", call_id="call", tool_name="browser_commit", args={}, reason="fresh review"
    )
    response = await enabled.request_permission(request)
    assert response.decision == "allow"
    assert response.source == "send_it"
    assert response.grant is None
    permission.assert_not_called()
    assert await normal.request_permission(request) is permission.return_value
    permission.assert_awaited_once_with(request)
    enabled.enabled = False
    assert await enabled.request_permission(request) is permission.return_value
    assert permission.await_count == 2
    enabled.enabled = True
    assert (await enabled.request_permission(request)).source == "send_it"
    assert permission.await_count == 2
    assert normal.enabled is False


@pytest.mark.asyncio
async def test_workflow_confirmation_bypasses_but_selection_still_requests_data() -> None:
    approval = AsyncMock(return_value=ApprovalResponse(selected_keys=["second"]))
    permissions = ChatPermissions(
        permission_responder=AsyncMock(),
        approval_responder=approval,
        destination_responder=AsyncMock(),
        enabled=True,
    )
    confirm = ApprovalRequest(run_id="run", step_id="confirm", mode="confirm", prompt="Proceed?")
    automatic = await permissions.request_workflow_approval(confirm)
    assert automatic.approved is True
    assert automatic.source == "send_it"
    assert ApprovalResponse.model_validate_json(automatic.model_dump_json()) == automatic
    approval.assert_not_called()
    select = ApprovalRequest(
        run_id="run",
        step_id="select",
        mode="select",
        prompt="Choose",
        item_keys=["first", "second"],
    )
    assert await permissions.request_workflow_approval(select) is approval.return_value
    approval.assert_awaited_once_with(select)
    permissions.enabled = False
    assert await permissions.request_workflow_approval(confirm) is approval.return_value
    assert approval.await_count == 2


@pytest.mark.asyncio
async def test_destination_bypass_is_one_occurrence_only_and_reversible() -> None:
    destination = AsyncMock(return_value=DestinationApprovalResponse(decision="deny"))
    permissions = ChatPermissions(
        permission_responder=AsyncMock(),
        approval_responder=AsyncMock(),
        destination_responder=destination,
        enabled=True,
    )
    request = DestinationApprovalRequest(
        ref=ProfileResourceRef(profile="personal", name="login"),
        revision=1,
        field="password",
        label="Login",
        top_level_origin="https://example.com",
        frame_origin="https://example.com",
        occurrence="once",
        execution_mode="foreground",
    )
    assert (await permissions.request_protected_destination(request)).decision == "allow_once"
    destination.assert_not_called()
    permissions.enabled = False
    assert await permissions.request_protected_destination(request) is destination.return_value
    destination.assert_awaited_once_with(request)


def test_permission_response_source_defaults_for_existing_serialized_responses() -> None:
    response = PermissionResponse.model_validate_json('{"decision":"allow","grant":"scoped"}')
    assert response.source == "user"
    assert PermissionResponse.model_validate_json(response.model_dump_json()) == response
    automatic = PermissionResponse(decision="allow", source="send_it")
    assert PermissionResponse.model_validate_json(automatic.model_dump_json()) == automatic


@pytest.mark.asyncio
@pytest.mark.parametrize("send_it", [False, True])
@pytest.mark.parametrize("mode", ["confirm", "select"])
@pytest.mark.parametrize("user_approves", [False, True])
async def test_workflow_records_approval_provenance_without_changing_output(
    send_it: bool,
    mode: str,
    user_approves: bool,
) -> None:
    from ricky.agent.events import WorkflowEvent
    from ricky.agent.session import AgentSession
    from ricky.agent.workflow import WorkflowRunner
    from ricky.config import RickySettings
    from ricky.tools import ToolRegistry
    from ricky.workflows.compile import compile_workflow
    from ricky.workflows.run import WorkflowSourceIdentity
    from ricky.workflows.spec import WorkflowSpec

    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    registry = ToolRegistry([])
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "approval-evidence",
            "description": "Approval provenance fixture",
            "steps": [
                {
                    "id": "confirm",
                    "kind": "approval",
                    "mode": mode,
                    "prompt": "Proceed?",
                    **(
                        {"proposal": "proposed work"}
                        if mode == "confirm"
                        else {"collection": ["first", "second"], "item_key": {"ref": "item.source"}}
                    ),
                }
            ],
        }
    )
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors
    approve = AsyncMock(
        return_value=ApprovalResponse(
            approved=user_approves, selected_keys=["second"] if user_approves else []
        )
    )
    permissions = ChatPermissions(
        permission_responder=AsyncMock(),
        approval_responder=approve,
        destination_responder=AsyncMock(),
        enabled=send_it,
    )
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=session,
        approval_responder=permissions.request_workflow_approval,
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="approval-evidence"),
            path="/tmp/approval-evidence/workflow.toml",
            scope="fixture",
            content_digest="fixture",
        ),
    )
    run = await runner.start({})
    automatic = send_it and mode == "confirm"
    approved = automatic or user_approves
    assert run.status == ("failed" if mode == "confirm" and not approved else "completed")
    if mode == "confirm":
        assert run.steps["confirm"].output == (
            {"approved": True, "proposal": "proposed work"} if approved else None
        )
    else:
        assert run.steps["confirm"].output == {
            "approved": ["second"] if user_approves else [],
            "rejected": ["first"] if user_approves else ["first", "second"],
            "selected_keys": ["second"] if user_approves else [],
        }
    decisions = [
        event
        for event in runner.events
        if isinstance(event, WorkflowEvent) and event.action == "approval_decided"
    ]
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.step_id == "confirm"
    assert decision.details == {
        "source": "send_it" if automatic else "user",
        "approved": approved,
        "mode": mode,
    }
    if mode == "select":
        assert decision.reason == "selection supplied by user"
    else:
        assert decision.reason == (
            "approved by chat send-it mode"
            if automatic
            else "approved by user"
            if approved
            else "denied by user"
        )
    assert WorkflowEvent.model_validate_json(decision.model_dump_json()) == decision
    assert approve.await_count == (0 if automatic else 1)
