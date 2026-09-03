"""Task-scoped effect authority with cycle-safe public exports."""

# ruff: noqa: F401 - TYPE_CHECKING imports preserve the public type surface.

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ricky.authority.compiler import (
        ContractAuthorityCompiler,
        build_grant_source,
        principal_id,
    )
    from ricky.authority.engine import (
        DelegatedAuthorityError,
        DelegatedEffectTool,
        DelegatedRun,
        build_delegated_tools,
    )
    from ricky.authority.registry import (
        AuthorityEvaluator,
        AuthorityRegistry,
        AuthorityRegistryError,
        default_authority_registry,
    )
    from ricky.authority.store import (
        AuthorityStore,
        AuthorityStoreError,
        GrantNotFoundError,
        GrantStateError,
    )
    from ricky.authority.types import (
        AuthorityScope,
        AuthorityVerdict,
        DelegationGrant,
        GrantActivity,
        GrantSource,
        source_text_digest,
    )

_EXPORTS = {
    "ContractAuthorityCompiler": (
        "ricky.authority.compiler",
        "ContractAuthorityCompiler",
    ),
    "build_grant_source": ("ricky.authority.compiler", "build_grant_source"),
    "principal_id": ("ricky.authority.compiler", "principal_id"),
    "DelegatedAuthorityError": ("ricky.authority.engine", "DelegatedAuthorityError"),
    "DelegatedEffectTool": ("ricky.authority.engine", "DelegatedEffectTool"),
    "DelegatedRun": ("ricky.authority.engine", "DelegatedRun"),
    "build_delegated_tools": ("ricky.authority.engine", "build_delegated_tools"),
    "AuthorityEvaluator": ("ricky.authority.registry", "AuthorityEvaluator"),
    "AuthorityRegistry": ("ricky.authority.registry", "AuthorityRegistry"),
    "AuthorityRegistryError": ("ricky.authority.registry", "AuthorityRegistryError"),
    "default_authority_registry": (
        "ricky.authority.registry",
        "default_authority_registry",
    ),
    "AuthorityStore": ("ricky.authority.store", "AuthorityStore"),
    "AuthorityStoreError": ("ricky.authority.store", "AuthorityStoreError"),
    "GrantNotFoundError": ("ricky.authority.store", "GrantNotFoundError"),
    "GrantStateError": ("ricky.authority.store", "GrantStateError"),
    "AuthorityScope": ("ricky.authority.types", "AuthorityScope"),
    "AuthorityVerdict": ("ricky.authority.types", "AuthorityVerdict"),
    "DelegationGrant": ("ricky.authority.types", "DelegationGrant"),
    "GrantActivity": ("ricky.authority.types", "GrantActivity"),
    "GrantSource": ("ricky.authority.types", "GrantSource"),
    "source_text_digest": ("ricky.authority.types", "source_text_digest"),
}

__all__ = [
    "AuthorityEvaluator",
    "AuthorityRegistry",
    "AuthorityRegistryError",
    "AuthorityScope",
    "AuthorityStore",
    "AuthorityStoreError",
    "AuthorityVerdict",
    "ContractAuthorityCompiler",
    "DelegatedAuthorityError",
    "DelegatedEffectTool",
    "DelegatedRun",
    "DelegationGrant",
    "GrantActivity",
    "GrantNotFoundError",
    "GrantSource",
    "GrantStateError",
    "build_delegated_tools",
    "build_grant_source",
    "default_authority_registry",
    "principal_id",
    "source_text_digest",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value
