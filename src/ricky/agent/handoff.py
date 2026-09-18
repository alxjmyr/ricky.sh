"""Provider-neutral evidence and wording for accepted background work."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BackgroundHandoff(BaseModel):
    """Durably admitted work awaiting its foreground acknowledgement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)

    @field_validator("request_id", "title")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("handoff fields must not be blank")
        return value


def background_handoff_acknowledgement(handoffs: Sequence[BackgroundHandoff]) -> str:
    """Render admitted titles without exposing internal execution identities."""
    titles = [" ".join(item.title.split()) for item in handoffs]
    if not titles:
        raise ValueError("an acknowledgement requires admitted work")
    if len(titles) == 1:
        return f"I'll run this in the background and report back here: {titles[0]}"
    return "I'll run these tasks in the background and report back here:\n" + "\n".join(
        f"- {title}" for title in titles
    )
