# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""CI helper for the macOS lifecycle e2e (.github/workflows/e2e.yml).

Not shipped. Builds structurally-real ``.app`` bundles / manifests and reads
their version, so the workflow YAML stays readable. Kept out of ``tests/``
because it's a CLI invoked from a workflow step, not collected by pytest.

    app <path> <version> <executable> <marker>
    manifest <path> <version> <companion_hash> <sa_hash>
    appversion <app_path>
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

ASSET_STEM = "locai-link-llm-stt"


def make_app(path: str, version: str, executable: str, marker: str) -> None:
    p = Path(path)
    (p / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (p / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (p / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "CFBundleExecutable": executable,
                "CFBundleIdentifier": "uk.co.locai.link.companion",
            }
        )
    )
    exe = p / "Contents" / "MacOS" / executable
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    (p / "Contents" / "Resources" / "build-marker.txt").write_text(marker, encoding="utf-8")


def make_manifest(path: str, version: str, companion_hash: str, sa_hash: str) -> None:
    Path(path).write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "asset_name": ASSET_STEM,
                "version": version,
                "git_sha": "ci",
                "built_at": "ci",
                "plugins": [],
                "apps": {"companion": companion_hash, "setup_assistant": sa_hash},
            }
        ),
        encoding="utf-8",
    )


def app_version(app_path: str) -> None:
    data = plistlib.loads((Path(app_path) / "Contents" / "Info.plist").read_bytes())
    print(str(data["CFBundleShortVersionString"]))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    dispatch = {
        "app": lambda: make_app(*rest),
        "manifest": lambda: make_manifest(*rest),
        "appversion": lambda: app_version(*rest),
    }
    fn = dispatch.get(cmd)
    if fn is None:
        sys.exit(f"unknown subcommand: {cmd}")
    fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
