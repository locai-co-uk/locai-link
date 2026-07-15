# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Serve a locally-built tarball as a stand-in GitHub "latest release", so OTA
(including whole-app updates) can be exercised end to end without cutting a real
release.

Pairs with the ``LOCAI_RELEASES_API_BASE`` / ``LOCAI_LATEST_VERSION`` overrides
the updater honours. A frozen production bundle ignores those unless
``LOCAI_ALLOW_OTA_OVERRIDES`` is also set, so the env can't redirect a real
device's OTA. Typical loop:

    # 1. Build the target version (bump pyproject.toml first, tweak the
    #    companion if you want to see it swap), naming it cleanly:
    ./build.tmp.sh --release

    # 2. Serve it:
    python3 bundling/serve_local_release.py dist/locai-link-llm-stt-linux-x86_64-v1.1.1.tar.gz

    # 3. Run the *installed* frozen agent pointed at the local server, then
    #    trigger the update (loopback /update or the companion button):
    LOCAI_ALLOW_OTA_OVERRIDES=1 LOCAI_RELEASES_API_BASE=http://localhost:8765 \
        LOCAI_LATEST_VERSION=1.1.1 ~/.local/share/locai/locai-link run
    curl -XPOST localhost:20505/update
"""

import argparse
import hashlib
import http.server
import json
import re
import socketserver
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tarball", help="path to a built OTA tarball")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", default="locai-co-uk/locai-link")
    args = parser.parse_args()

    tar = Path(args.tarball).resolve()
    # Strip a local -DEV suffix so the asset name matches the updater's regex.
    asset = tar.name.replace("-DEV.tar.gz", ".tar.gz")
    match = re.search(r"-v(\d+\.\d+\.\d+)\.tar\.gz$", asset)
    if not match:
        raise SystemExit(f"cannot parse version from asset name: {asset}")
    version = match.group(1)
    sha_name = f"{asset}.sha256"
    tar_bytes = tar.read_bytes()
    sha_body = f"{hashlib.sha256(tar_bytes).hexdigest()}  {asset}\n".encode()

    base = f"http://localhost:{args.port}"
    latest_json = json.dumps(
        {
            "tag_name": f"v{version}",
            "assets": [
                {"name": asset, "browser_download_url": f"{base}/{asset}"},
                {"name": sha_name, "browser_download_url": f"{base}/{sha_name}"},
            ],
        }
    ).encode()

    routes: dict[str, tuple[bytes, str]] = {
        f"/repos/{args.repo}/releases/latest": (latest_json, "application/json"),
        f"/{asset}": (tar_bytes, "application/gzip"),
        f"/{sha_name}": (sha_body, "text/plain"),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # quiet
            pass

        def do_GET(self) -> None:  # noqa: N802
            hit = routes.get(self.path)
            if hit is None:
                self.send_error(404, "not served")
                return
            body, content_type = hit
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"serving {asset} (v{version}) at {base}")
    print(f"point the agent at it:\n  LOCAI_RELEASES_API_BASE={base} LOCAI_LATEST_VERSION={version}")
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
