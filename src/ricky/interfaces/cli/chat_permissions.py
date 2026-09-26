"""Resident chat's optional, reversible approval-prompt bypass."""

from __future__ import annotations

from ricky.agent.events import PermissionRequestedEvent
from ricky.agent.tool_dispatch import PermissionResponder
from ricky.agent.workflow import ApprovalRequest, ApprovalResponder, ApprovalResponse
from ricky.permissions import PermissionResponse
from ricky.protected_values import (
    DestinationApprovalRequest,
    DestinationApprovalResponder,
    DestinationApprovalResponse,
)


class ChatPermissions:
    """Keep send-it authority local to one live chat and out of saved grants."""

    def __init__(
        self,
        *,
        permission_responder: PermissionResponder,
        approval_responder: ApprovalResponder,
        destination_responder: DestinationApprovalResponder,
        enabled: bool = False,
    ) -> None:
        self.enabled = enabled
        self._permission_responder = permission_responder
        self._approval_responder = approval_responder
        self._destination_responder = destination_responder

    async def request_permission(self, event: PermissionRequestedEvent) -> PermissionResponse:
        """Answer a validated, policy-reviewable call without remembering a grant."""
        if self.enabled:
            return PermissionResponse(decision="allow", source="send_it")
        return await self._permission_responder(event)

    async def request_workflow_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """Confirm checkpoints while retaining requests for actual selection data."""
        if self.enabled and request.mode == "confirm":
            return ApprovalResponse(approved=True, source="send_it")
        return await self._approval_responder(request)

    async def request_protected_destination(
        self, request: DestinationApprovalRequest
    ) -> DestinationApprovalResponse:
        """Authorize this destination occurrence without persisting destination policy."""
        if self.enabled:
            return DestinationApprovalResponse(decision="allow_once")
        return await self._destination_responder(request)
