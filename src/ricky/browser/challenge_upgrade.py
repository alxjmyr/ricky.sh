"""Read-only upgrade inventory for browser-owned challenge metadata."""

from pathlib import Path

from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenges import ChallengeError
from ricky.browser.verification_store import VerificationClaimStore
from ricky.upgrades.models import AdapterTarget
from ricky.upgrades.non_sql import ReadOnlyFormatAdapter


class BrowserChallengesUpgradeAdapter(ReadOnlyFormatAdapter):
    """Version one is additive; existing installations have no challenge records."""

    def __init__(self, root: Path) -> None:
        target = AdapterTarget(
            adapter_id="browser_challenges",
            target_id="challenges",
            path=str(root),
            physical_path=str(root),
            kind="tree",
        )
        super().__init__(
            adapter_id="browser_challenges",
            targets=(target,),
            validators={target.target_id: self._validate},
            mutable=True,
        )

    @staticmethod
    def _validate(root: Path) -> None:
        if not root.is_dir():
            raise ValueError("browser challenge root is not a directory")
        for path in root.glob("*.json"):
            try:
                BrowserChallengeStore._decode(path)
            except ChallengeError as exc:
                raise ValueError("invalid browser challenge record") from exc
        sources = root / "sources"
        if sources.is_symlink():
            raise ValueError("verification claim directory cannot be a symlink")
        for path in sources.glob("*.json"):
            VerificationClaimStore.validate_file(path)
