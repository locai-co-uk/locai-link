#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
"""End-to-end dry-run of the bundle OTA primitives in ``src/link/app/updater.py``.

Builds a synthetic ``install_root`` + a local "release server" under a
tempdir, then walks the full OTA chain (discover -> latest_release_for ->
download -> verify -> extract -> health-check -> flip -> gc) using a real
in-process ``http.server``, real tarballs, and real subprocesses. No
PyInstaller bundle build required — this is a fast confidence check before
you hand the work off to CI.

Run from the repo root::

    uv run python bundling/_dryrun_ota.py
    uv run python bundling/_dryrun_ota.py --target /tmp/my-ota-inspection

Without ``--target`` the workspace is a tempdir that auto-deletes on exit.
With ``--target`` the workspace is created at the given path and kept after
the run, so you can poke at the resulting ``install_root`` / ``versions/`` /
pointer files. The path must not already contain anything (no accidental
overwrites).

Pass = exit 0 with every stage marked ``OK``; fail = the first failing
assertion prints the error and we exit nonzero.

The chain mirrors what ``UpdateAgentCommand`` will drive once the runtime
dispatch lands: identify self, resolve target release, download, verify,
extract, health-check, flip, GC.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import io
import json
import os
import socketserver
import stat
import sys
import tarfile
import tempfile
import textwrap
import threading
from contextlib import ExitStack
from pathlib import Path

# Wire the in-repo updater module onto sys.path the same way main.py does.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from link.app import updater  # noqa: E402

OK = "\033[32mOK\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36m••\033[0m"


def say(stage: str, status: str = OK, detail: str = "") -> None:
    print(f"  [{status}] {stage}" + (f" — {detail}" if detail else ""))


def _write_synthetic_bundle(version_dir: Path, version: str, *, asset_name: str) -> None:
    """Drop a manifest.json + a runnable locai-link-runtime stub."""
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / updater.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "asset_name": asset_name,
                "version": version,
                "git_sha": "dryrun",
                "built_at": "2026-06-22T00:00:00Z",
                "plugins": [{"name": "language_model", "version": "0.1.0"}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # A bash stub that hand-rolls a `self-check` response so health_check has a
    # real binary to spawn. Returns 0 immediately; that's enough for the flip.
    runtime = version_dir / updater.RUNTIME_BINARY
    runtime.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # synthetic locai-link-runtime (_dryrun_ota.py) for {version}
            if [ "$1" = "self-check" ]; then
                echo "self-check: ok (synthetic {version})" >&2
                exit 0
            fi
            echo "synthetic runtime {version} — no-op" >&2
            """
        ),
        encoding="utf-8",
    )
    runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_release_tarball(version: str, *, asset_name: str) -> bytes:
    """Build a versions/<v>/... tarball mirroring the on-disk shape build.py produces."""
    bundle_buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as td:
        version_dir = Path(td) / "x"
        _write_synthetic_bundle(version_dir, version, asset_name=asset_name)
        with tarfile.open(fileobj=bundle_buf, mode="w:gz") as tf:
            for entry in version_dir.iterdir():
                tf.add(entry, arcname=f"versions/{version}/{entry.name}")
    return bundle_buf.getvalue()


class _ServeDir(socketserver.TCPServer):
    allow_reuse_address = True


def _serve(directory: Path) -> tuple[str, "_ServeDir", threading.Thread]:
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a, **kw):  # silence
            pass

    server = _ServeDir(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_address[1]}", server, thread


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="End-to-end dry-run of the bundle OTA primitives.")
    p.add_argument(
        "--target",
        type=Path,
        default=None,
        help=(
            "Workspace location. If given, the dir is created and kept after the "
            "run so you can inspect the resulting install_root. Refuses to "
            "overwrite a non-empty existing dir. Omit for an auto-cleaned tempdir."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asset_stem = "locai-link-llm-linux-x86_64"
    # Match the current repo version in pyproject.toml and the next bump.
    old_version = "1.0.15"
    new_version = "1.0.16"
    new_tarball_name = f"{asset_stem}-v{new_version}.tar.gz"

    with ExitStack() as stack:
        if args.target is not None:
            td = args.target.resolve()
            if td.exists() and any(td.iterdir()):
                print(
                    f"target {td} exists and is not empty — refusing to overwrite",
                    file=sys.stderr,
                )
                return 1
            td.mkdir(parents=True, exist_ok=True)
            persistent = True
        else:
            td = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dryrun-ota-")))
            persistent = False
        print(f"\n{INFO} workspace: {td}" + ("  (kept after exit)" if persistent else ""))

        # ---- 1) Set up the "deployed" install_root with v1.0.15 current ----
        install_root = td / "install_root"
        install_root.mkdir()
        _write_synthetic_bundle(
            install_root / updater.VERSIONS_DIR / old_version,
            old_version,
            asset_name=asset_stem,
        )
        (install_root / updater.CURRENT_LINK).symlink_to(
            Path(updater.VERSIONS_DIR) / old_version, target_is_directory=True
        )
        say(f"install_root prepared at {old_version}", detail=str(install_root))

        # ---- 2) Build the "release" — tarball + .sha256, served over HTTP ----
        release_dir = td / "release_assets"
        release_dir.mkdir()
        tar_bytes = _make_release_tarball(new_version, asset_name=asset_stem)
        (release_dir / new_tarball_name).write_bytes(tar_bytes)
        sha = hashlib.sha256(tar_bytes).hexdigest()
        (release_dir / f"{new_tarball_name}.sha256").write_text(f"{sha}  {new_tarball_name}\n", encoding="utf-8")
        asset_url, asset_server, _ = _serve(release_dir)
        say(f"release server up at {asset_url}", detail=new_tarball_name)

        # ---- 3) Fake the GitHub releases/latest API response ----
        api_root = td / "api_root"
        (api_root / "repos" / "dryrun" / "dryrun" / "releases").mkdir(parents=True)
        (api_root / "repos" / "dryrun" / "dryrun" / "releases" / "latest").write_text(
            json.dumps(
                {
                    "tag_name": f"v{new_version}",
                    "assets": [
                        {
                            "name": new_tarball_name,
                            "browser_download_url": f"{asset_url}/{new_tarball_name}",
                        },
                        {
                            "name": f"{new_tarball_name}.sha256",
                            "browser_download_url": f"{asset_url}/{new_tarball_name}.sha256",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        api_url, api_server, _ = _serve(api_root)
        say(f"github api stub at {api_url}")

        try:
            # ---- 4) discover_install_root from a path inside it ----
            inferred = updater.discover_install_root(
                install_root / updater.VERSIONS_DIR / old_version / updater.RUNTIME_BINARY
            )
            assert inferred == install_root, f"{inferred} != {install_root}"
            say("discover_install_root")

            # ---- 5) read_manifest ----
            m = updater.read_manifest(install_root)
            assert m.version == old_version
            say("read_manifest", detail=f"v{m.version} / {m.asset_name}")

            # ---- 6) latest_release_for ----
            info = updater.latest_release_for(asset_stem, repo="dryrun/dryrun", api_base=api_url)
            assert info.version == new_version
            assert info.sha256_url is not None
            say("latest_release_for", detail=f"-> {info.tag} ({info.asset_name})")

            # ---- 7) download ----
            staging = updater.staging_path(install_root)
            downloaded = updater.download(
                info.download_url,
                staging / info.asset_name,
            )
            assert downloaded.read_bytes() == tar_bytes
            say("download", detail=f"{downloaded.stat().st_size} bytes")

            # ---- 8) verify (mismatch first, then match) ----
            try:
                updater.verify(downloaded, expected_sha256="00" * 32)
            except updater.VerifyFailed:
                say("verify (mismatch correctly rejected)")
            else:
                say("verify (mismatch NOT rejected)", status=FAIL)
                return 1
            updater.verify(downloaded, expected_sha256_url=info.sha256_url)
            say("verify (sha match)")

            # ---- 9) extract ----
            target = install_root / updater.VERSIONS_DIR / new_version
            updater.extract(downloaded, target)
            assert (target / updater.MANIFEST_NAME).is_file()
            assert (target / updater.RUNTIME_BINARY).is_file()
            say("extract", detail=str(target.relative_to(install_root)))

            # ---- 10) health_check ----
            ok = updater.health_check(target / updater.RUNTIME_BINARY, timeout=10.0)
            if not ok:
                say("health_check (synthetic runtime failed)", status=FAIL)
                return 1
            say("health_check")

            # ---- 11) flip_current ----
            updater.flip_current(install_root, new_version)
            assert (install_root / updater.CURRENT_LINK).is_symlink()
            assert (install_root / updater.CURRENT_LINK).resolve().name == new_version
            assert (install_root / updater.PREVIOUS_LINK).is_symlink()
            assert (install_root / updater.PREVIOUS_LINK).resolve().name == old_version
            say("flip_current", detail=f"{old_version} -> {new_version}, previous preserved")

            # ---- 12) gc_old_versions (keep current + previous only) ----
            # Drop in a third older version so gc has something to delete.
            old2 = "1.0.10"
            _write_synthetic_bundle(install_root / updater.VERSIONS_DIR / old2, old2, asset_name=asset_stem)
            removed = updater.gc_old_versions(install_root)
            assert old2 in removed, f"expected to gc {old2}, got {removed}"
            assert (install_root / updater.VERSIONS_DIR / new_version).is_dir()
            assert (install_root / updater.VERSIONS_DIR / old_version).is_dir()
            say("gc_old_versions", detail=f"removed {removed}")

            # ---- 13) clear_staging ----
            updater.clear_staging(install_root)
            assert not (install_root / updater.STAGING_DIR).exists()
            say("clear_staging")

            # ---- 14) Final layout snapshot ----
            print(f"\n{INFO} final install_root layout:")
            for p in sorted(install_root.rglob("*")):
                rel = p.relative_to(install_root)
                kind = "L" if p.is_symlink() else ("D" if p.is_dir() else "F")
                target_arrow = f" -> {os.readlink(p)}" if p.is_symlink() else ""
                print(f"    {kind} {rel}{target_arrow}")

            print(f"\n{OK} OTA dry-run completed end-to-end.")
            return 0

        finally:
            for s in (asset_server, api_server):
                s.shutdown()
                s.server_close()


if __name__ == "__main__":
    sys.exit(main())
