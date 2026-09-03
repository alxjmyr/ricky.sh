"""Permission policy models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PermissionDecision = Literal["allow", "deny", "ask"]


class PolicyRule(BaseModel):
    """One ordered permission policy rule."""

    tool_name: str
    decision: PermissionDecision
    params_equal: dict[str, Any] = Field(default_factory=dict)
    reason: str = "matched policy rule"

    def matches(self, tool_name: str, params: dict[str, Any]) -> bool:
        """Return true when this rule covers a tool invocation."""
        if self.tool_name != tool_name:
            return False
        return all(params.get(key) == value for key, value in self.params_equal.items())


class Policy(BaseModel):
    """Ordered policy rules plus risk defaults."""

    rules: list[PolicyRule] = Field(default_factory=list)
    read_only_default: PermissionDecision = "allow"
    mutating_default: PermissionDecision = "ask"
    destructive_default: PermissionDecision = "ask"


class GrantScope(BaseModel):
    """A tool's declaration of how a remembered grant may generalize one call.

    Built from the *current* call args: ``params_equal`` keeps only the
    policy-relevant params (identity params like a message id are dropped), so a
    remembered grant matches later calls that differ only by identity.
    """

    model_config = ConfigDict(extra="forbid")

    params_equal: dict[str, Any] = Field(default_factory=dict)
    label: str
    allow_unconstrained: bool = False
    requires_permission: bool = False
    directory_param: str | None = None
    directory_path: str | None = None
    directory_label: str | None = None

    @model_validator(mode="after")
    def _directory_scope_is_complete_and_canonical(self) -> GrantScope:
        values = (self.directory_param, self.directory_path, self.directory_label)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("directory grant scope requires param, path, and label")
        if self.directory_path is not None:
            path = Path(self.directory_path)
            if not path.is_absolute() or ".." in path.parts or path != path.resolve():
                raise ValueError("directory grant path must be canonical and absolute")
        return self


class GrantOption(BaseModel):
    """One offered "remember this" choice, computed by the loop.

    Carried on the request event so the interface stays a pure renderer: it maps
    an option to a key and returns the chosen ``id``; it never decides breadth.
    """

    id: str
    label: str


class PermissionResponse(BaseModel):
    """Interface response to an ask decision."""

    decision: Literal["allow", "deny"]
    grant: str | None = None
    """Chosen ``GrantOption.id`` to remember, or ``None`` for allow-once/deny."""
