"""Package current user documentation for normal and editable wheel builds."""

import runpy
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        helper = runpy.run_path(str(root / "scripts" / "bundle_docs.py"))
        references = helper["synchronize"](root)
        build_data["force_include"][str(references)] = "ricky/builtins/skills/ricky-docs/references"
