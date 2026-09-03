"""Shared model-facing types for Google integration tools."""

from typing import Annotated

from pydantic import Field

GoogleAccountId = Annotated[
    str,
    Field(
        description=(
            "Exact profile-qualified Google account id from Accessible resources, "
            "such as personal/personal or work/company; never use an email address "
            "or an unqualified name."
        )
    ),
]

__all__ = ["GoogleAccountId"]
