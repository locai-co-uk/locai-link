# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Render the final ``boot.json`` for a package.

``boot.json``'s ``plugin_set`` must match what the bundle actually contains,
because the launcher derives the first-launch fetch asset name from it
(``locai-link-<codes>-<os>-<arch>``). Shipping a static ``plugin_set`` means any
build that isn't the hardcoded profile fetches a non-existent asset on
bootstrap.

This takes the static template (host_app / channel / asset_repo) and injects
``plugin_set`` derived from the bundle's ``manifest.json`` plugins, mapped to
short codes via the single source of truth in ``manifest.py`` (PLUGIN_CODES).

Usage::

    python3 bundling/gen_boot_json.py \\
        --manifest dist/locai-link/current/manifest.json \\
        --template bundling/pkg/boot.json \\
        --output   <staging>/boot.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import PLUGIN_CODES, PLUGIN_ORDER  # noqa: E402


def plugin_set_from_manifest(manifest: dict) -> list[str]:
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
    boot["plugin_set"] = plugin_set_from_manifest(manifest)

    Path(args.output).write_text(json.dumps(boot, indent=2) + "\n", encoding="utf-8")
    print(f"boot.json written to {args.output} (plugin_set={boot['plugin_set']})")


if __name__ == "__main__":
    main()
