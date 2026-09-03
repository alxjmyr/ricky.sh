"""Logical route policy and trusted conversation-route resolution."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.config import RickySettings
from ricky.profiles import SHARED_PROFILE, ProfileLabel, ProfileName, validate_profile_name


class RouteError(ValueError):
    """A logical route is unknown or violates profile-clearance policy."""


class ResolvedRoute(BaseModel):
    """Trusted transport destination returned only to a delivery worker."""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(min_length=1, max_length=200)
    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    destination_ref: str = Field(min_length=1, max_length=500)
    owner_profile: ProfileName
    accepted_profiles: list[ProfileName] = Field(min_length=1)

    @field_validator("owner_profile")
    @classmethod
    def _owner_profile(cls, value: str) -> str:
        return validate_profile_name(value)

    @field_validator("accepted_profiles")
    @classmethod
    def _accepted_profiles(cls, values: list[str]) -> list[str]:
        normalized = [validate_profile_name(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("resolved route accepted_profiles must be unique")
        return sorted(normalized, key=lambda item: (item != SHARED_PROFILE, item))

    @model_validator(mode="after")
    def _owner_has_clearance(self) -> ResolvedRoute:
        if self.owner_profile not in self.accepted_profiles:
            raise ValueError("resolved route owner_profile must be accepted by the route")
        return self


class ConversationRouteResolver(Protocol):
    """Trusted gateway-owned lookup for dynamic conversation routes."""

    async def resolve_conversation_route(
        self,
        conversation_id: str,
        profile_label: ProfileLabel,
    ) -> ResolvedRoute:
        """Resolve one trusted conversation record without model-supplied destination data."""

        ...


class RoutePolicy:
    """Resolve configured route names and enforce their profile clearance."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        conversation_resolver: ConversationRouteResolver | None = None,
    ) -> None:
        self._settings = settings.messaging
        self._conversation_resolver = conversation_resolver

    async def resolve(self, route: str, profile_label: ProfileLabel) -> ResolvedRoute:
        route = route.strip()
        if not route:
            raise RouteError("notification route cannot be empty")
        if route.startswith("conversation:"):
            return await self._resolve_conversation(route, profile_label)
        configured = self._settings.routes.get(route)
        if configured is None:
            raise RouteError(f"unknown notification route: {route}")
        rejected = sorted(set(profile_label.required_profiles) - set(configured.accepted_profiles))
        if rejected:
            raise RouteError(
                f"notification requiring profile(s) {', '.join(rejected)} cannot use "
                f"route {route!r}"
            )
        transport = self._settings.transports[configured.transport]
        return ResolvedRoute(
            route=route,
            transport=transport.type,
            account=transport.account,
            destination_ref=configured.destination,
            owner_profile=configured.owner_profile,
            accepted_profiles=list(configured.accepted_profiles),
        )

    async def validate(self, route: str, profile_label: ProfileLabel) -> None:
        """Validate route policy without exposing the resolved destination."""

        await self.resolve(route, profile_label)

    async def _resolve_conversation(
        self,
        route: str,
        profile_label: ProfileLabel,
    ) -> ResolvedRoute:
        conversation_id = route.removeprefix("conversation:").strip()
        if not conversation_id:
            raise RouteError("conversation route requires a conversation id")
        if self._conversation_resolver is None:
            raise RouteError("conversation routes require a trusted conversation resolver")
        resolved = await self._conversation_resolver.resolve_conversation_route(
            conversation_id,
            profile_label,
        )
        if resolved.route != route:
            raise RouteError("conversation resolver returned a mismatched route")
        rejected = sorted(set(profile_label.required_profiles) - set(resolved.accepted_profiles))
        if rejected:
            raise RouteError(
                "notification cannot resolve a conversation that rejects required "
                f"profile(s): {', '.join(rejected)}"
            )
        return resolved
