# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Render the final ``boot.json`` for a package.

``boot.json``'s ``shape`` must match the bundle, because the launcher derives the
first-launch fetch asset name from it (``locai-link-<shape>-<os>-<arch>``). This
injects ``shape`` (and ``plugin_set`` as metadata) from the bundle's
``manifest.json`` into the static template (host_app / channel / asset_repo).

Usage::

    python3 bundling/gen_boot_json.py \\
        --manifest dist/locai-link/current/manifest.json \\
        --template bundling/boot.json \\
        --output   <staging>/boot.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import PLUGIN_CODES, PLUGIN_ORDER  # noqa: E402


def plugin_set_from_manifest(manifest: dict[str, Any]) -> list[str]:
    """Ordered short codes for the plugins listed in a bundle manifest."""
    names = {p.get("name") for p in manifest.get("plugins", []) if p.get("name")}
    unknown = names - set(PLUGIN_CODES)
    if unknown:
        raise SystemExit(f"Plugin(s) missing a code in manifest.py PLUGIN_CODES: {', '.join(sorted(unknown))}")
    return [PLUGIN_CODES[name] for name in PLUGIN_ORDER if name in names]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="bundle manifest.json path")
    parser.add_argument("--template", required=True, help="static boot.json template path")
    parser.add_argument("--output", required=True, help="where to write the rendered boot.json")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    boot = json.loads(Path(args.template).read_text(encoding="utf-8"))
    # Legacy manifests without a shape default to desktop; anything else must be
    # a known shape, else boot.json would request an asset no release publishes.
    shape = manifest.get("shape", "desktop")
    if shape not in ("desktop", "headless"):
        raise SystemExit(f"Unsupported bundle shape in manifest: {shape!r}")
    boot["shape"] = shape
    boot["plugin_set"] = plugin_set_from_manifest(manifest)

    Path(args.output).write_text(json.dumps(boot, indent=2) + "\n", encoding="utf-8")
    print(f"boot.json written to {args.output} (shape={boot['shape']}, plugin_set={boot['plugin_set']})")


if __name__ == "__main__":
    main()
