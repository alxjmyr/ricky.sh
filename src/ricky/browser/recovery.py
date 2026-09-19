"""Safe recovery for reference errors rejected before browser dispatch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from ricky.browser.types import BrowserError, BrowserFailure
from ricky.tool_contracts import ToolRuntimeFailure


@dataclass(frozen=True)
class SessionReferences:
    """In-process snapshot of handles owned by one browser runtime."""

    session_id: str
    resource: str
    selected_page_id: str | None
    page_ids: tuple[str, ...]


class BrowserReferenceError(BrowserError):
    """A deterministic reference rejection with locally issued recovery evidence."""

    def __init__(
        self,
        code: Literal["unknown_session", "unknown_page", "session_limit"],
        references: tuple[SessionReferences, ...],
        *,
        session_limit: int,
    ) -> None:
        message = {
            "unknown_session": "unknown browser session id",
            "unknown_page": "unknown browser page id",
            "session_limit": "the configured live browser session limit has been reached",
        }[code]
        guidance = (
            "Reuse an existing session below only if it belongs to the intended resource; "
            "opening another session will not repair an ID. "
            if code == "session_limit"
            else "Copy the exact session_id and matching page_id below; do not guess IDs. "
        )
        guidance += (
            "These are references in this runtime, not additional permissions. "
            "Use browser_pages with a listed session_id if more page references are needed."
        )
        rows = [asdict(item) for item in references]
        fingerprint = hashlib.sha256(
            json.dumps([code, session_limit, rows], sort_keys=True).encode()
        ).hexdigest()
        lines: list[str] = []
        for item in references:
            # The complete registry participates in the fingerprint, but feedback is bounded.
            row = asdict(item)
            row["page_ids"] = item.page_ids[:3]
            row["pages_omitted"] = max(0, len(item.page_ids) - 3)
            line = json.dumps(row, sort_keys=True)
            if sum(map(len, lines)) + len(line) > 1200:
                break
            lines.append(line)
        if lines:
            guidance += "\nCurrent runtime browser references:\n" + "\n".join(lines)
            if len(lines) != len(references):
                guidance += f"\nAdditional sessions omitted: {len(references) - len(lines)}."
        else:
            guidance += (
                "\nNo usable session references are currently registered. "
                "Do not repeat the unchanged call. "
            )
            guidance += (
                "The existing sessions are unavailable; stop and report the blocked runtime."
                if code == "session_limit"
                else "Open the intended authorized resource only if its opening tool is available."
            )
        super().__init__(BrowserFailure(code=code, message=f"{message}. {guidance}"))
        self.runtime_failure = ToolRuntimeFailure(
            kind="state_conflict", state_fingerprint=fingerprint, recovery=guidance
        )
