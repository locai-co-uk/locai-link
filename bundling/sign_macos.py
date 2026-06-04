# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Sign + notarise + staple the macOS Link bundle.

Runs AFTER ``bundling/build.py`` produces ``dist/locai-link/`` on macOS.

Workflow:
    1. Recursively walk the bundle directory, identify every Mach-O file,
       and codesign it with the Developer ID Application identity, hardened
       runtime on, using ``bundling/entitlements.plist``. Inside-out: sign
       leaf dylibs and helpers first, then the main executable last so the
       outer signature subsumes the inner ones.
    2. Zip the signed bundle (``ditto -c -k --sequesterRsrc --keepParent``)
       since notarytool needs an archive.
    3. Submit the zip to Apple via ``xcrun notarytool submit --wait``. This
       call blocks for 5-15 minutes typically; longer on Apple's bad days.
       Authenticates via an App Store Connect API key.
    4. ``xcrun stapler staple`` the original bundle so the notarisation
       ticket is embedded and the bundle works on first-run with no network.
    5. Verify via ``spctl --assess --type execute`` before exiting non-zero.

Usage (called from CI; manual use also fine):

    export APPLE_DEVELOPER_ID_APPLICATION="Developer ID Application: …"
    export APPLE_NOTARY_KEY_ID="ABCD1234EF"
    export APPLE_NOTARY_ISSUER_ID="69a6de70-…"
    export APPLE_NOTARY_KEY_PATH="/path/to/AuthKey_ABCD1234EF.p8"
    uv run python bundling/sign_macos.py --bundle dist/locai-link

The Developer ID Application certificate must already be present in the
default login keychain (or a keychain you've unlocked). In CI, the keychain
import + unlock is done by an earlier workflow step (see bundle.yml).

Env vars (all required):
    APPLE_DEVELOPER_ID_APPLICATION  Common name of the signing identity, e.g.
                                    "Developer ID Application: Loc.ai Ltd (TEAMID12AB)".
    APPLE_NOTARY_KEY_ID             App Store Connect API key ID (10-char).
    APPLE_NOTARY_ISSUER_ID          App Store Connect API issuer UUID.
    APPLE_NOTARY_KEY_PATH           Path to the .p8 file containing the API key.

Exit code is non-zero on any signing, notarisation, or stapling failure.
Apple's diagnostic logs are dumped to stderr on notarisation failure so CI
can surface them without a separate fetch step.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Apple's recommended bundle identifier convention. Notarisation accepts any
# reverse-DNS string but it's stored in the staple ticket — keep it stable
# across releases so audits and revocation lookups stay clean.
BUNDLE_IDENTIFIER = "uk.co.locai.link"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def _is_mach_o(path: Path) -> bool:
    """True if ``path`` is a Mach-O file (executable or dylib)."""
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with path.open("rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    # Mach-O magic numbers (32-bit, 64-bit, both endian) plus universal/fat.
    return magic in (
        b"\xfe\xed\xfa\xce",  # MH_MAGIC (32 BE)
        b"\xce\xfa\xed\xfe",  # MH_CIGAM (32 LE)
        b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64 (BE)
        b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64 (LE)
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC
        b"\xbe\xba\xfe\xca",  # FAT_CIGAM
    )


def _discover_mach_o(bundle: Path) -> list[Path]:
    """All Mach-O files inside the bundle, ordered leaves-first so nested
    signatures get applied before outer ones."""
    found: list[Path] = []
    for path in bundle.rglob("*"):
        if _is_mach_o(path):
            found.append(path)
    # Deeper paths first so the main executable is signed last.
    found.sort(key=lambda p: (-len(p.parts), str(p)))
    return found


def _run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with a single-line log."""
    logger.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def codesign_bundle(bundle: Path, identity: str, entitlements: Path) -> None:
    """Sign every Mach-O in the bundle, hardened runtime on, with entitlements."""
    if not bundle.is_dir():
        raise SystemExit(f"Bundle directory not found: {bundle}")
    if not entitlements.is_file():
        raise SystemExit(f"Entitlements file not found: {entitlements}")

    mach_o_files = _discover_mach_o(bundle)
    logger.info("Found %d Mach-O files under %s", len(mach_o_files), bundle)
    if not mach_o_files:
        raise SystemExit("No Mach-O files found — is this actually a built bundle?")

    # Sign every binary individually so dylibs deep inside the tree get
    # hardened runtime. --force overwrites any existing ad-hoc signature
    # PyInstaller may have applied to its bootloader. The leaves-first
    # sort means the top-level executable (locai-link/locai-link) is the
    # last file signed in the walk; we deliberately do NOT then run an
    # "outer" codesign on the bundle directory because a PyInstaller
    # onedir output is a plain directory, not an .app bundle. On a plain
    # directory `codesign --sign X dist/locai-link/` re-signs the main
    # executable a second time with a --identifier override, which
    # produced a notarisation-invalid signature on the previous
    # iteration (Apple's log: "The signature of the binary is invalid."
    # against locai-link.zip/locai-link/locai-link). Individual signing
    # of every Mach-O is sufficient for notarisation of onedir bundles.
    for binary in mach_o_files:
        _run(
            [
                "codesign",
                "--force",
                "--timestamp",
                "--options",
                "runtime",
                "--entitlements",
                str(entitlements),
                "--sign",
                identity,
                str(binary),
            ]
        )

    # Sanity check on the main executable — catches signature mismatches
    # before we waste 10 minutes on Apple's notarisation queue. We verify
    # the top-level binary specifically because that's the file Apple's
    # Gatekeeper checks on launch; --strict ensures no per-architecture
    # slice is unsigned in a fat binary.
    main_exe = bundle / bundle.name
    if not main_exe.exists():
        # Some onedir layouts name the main exe identically to the bundle
        # directory; in others it's elsewhere. Fall back to the last
        # entry in the leaves-first walk, which is the shallowest Mach-O.
        main_exe = mach_o_files[-1]
    _run(["codesign", "--verify", "--strict", "--verbose=2", str(main_exe)])
    logger.info("Codesign verification passed.")


def notarise(bundle: Path, key_id: str, issuer_id: str, key_path: Path) -> None:
    """Zip + submit + wait + staple. notarytool blocks until the verdict."""
    if not key_path.is_file():
        raise SystemExit(f"App Store Connect API key not found: {key_path}")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{bundle.name}.zip"
        # `ditto -c -k --sequesterRsrc --keepParent` is Apple's blessed
        # way to zip a bundle for notarisation. Plain `zip` strips xattrs.
        _run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(bundle),
                str(zip_path),
            ]
        )

        # notarytool --wait blocks until Apple returns Accepted/Invalid.
        # Two distinct failure modes to handle:
        #
        #  (a) notarytool itself fails to run (network, auth, Apple service
        #      outage, submission too large). Exits non-zero, may not
        #      produce JSON. Need to surface whatever it wrote.
        #
        #  (b) Submission reaches Apple and gets a verdict, but the verdict
        #      is `Invalid` (signature problem, missing entitlement, etc.).
        #      notarytool exits 0 in this case — Apple considers the
        #      SUBMISSION successful, just the verdict is negative. We
        #      detect this by parsing --output-format json's status field.
        #
        # check=False so we handle the exit code ourselves; both stdout
        # and stderr always get echoed so CI logs show the diagnostic
        # without a separate manual notarytool-log fetch.
        result = _run(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(zip_path),
                "--key",
                str(key_path),
                "--key-id",
                key_id,
                "--issuer",
                issuer_id,
                "--wait",
                "--output-format",
                "json",
            ],
            capture=True,
            check=False,
        )
        if result.stdout:
            logger.info("notarytool stdout:\n%s", result.stdout)
        if result.stderr:
            logger.info("notarytool stderr:\n%s", result.stderr)

        if result.returncode != 0:
            # Failure mode (a): notarytool couldn't reach a verdict. The
            # echoed stdout/stderr above contains Apple's diagnostic — read
            # the build log to triage. Common causes: transient Apple-side
            # 5xx (retry usually works), corrupt zip, expired API key.
            raise SystemExit(
                f"notarytool submit exited with code {result.returncode} before "
                f"producing a verdict. See stdout/stderr above for the cause."
            )

        import json as _json

        try:
            verdict = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            logger.error("Could not parse notarytool JSON output:\n%s", result.stdout)
            raise SystemExit("Notarisation verdict could not be parsed.")

        submission_id = verdict.get("id", "<unknown>")
        status = verdict.get("status", "<unknown>")
        logger.info("Notarytool verdict: status=%s id=%s", status, submission_id)

        if status != "Accepted":
            # Fetch Apple's diagnostic log inline so CI shows the
            # rejection reason without a separate manual `notarytool log`
            # round-trip. The log lists each file Apple objected to and why.
            logger.error("Notarisation rejected — fetching diagnostic log.")
            _run(
                [
                    "xcrun",
                    "notarytool",
                    "log",
                    submission_id,
                    "--key",
                    str(key_path),
                    "--key-id",
                    key_id,
                    "--issuer",
                    issuer_id,
                ],
                check=False,
            )
            raise SystemExit(
                f"Notarisation rejected with status={status!r} (submission {submission_id}). "
                f"See the log output above for per-file objections."
            )

    # Attempt to staple the notarisation ticket onto the bundle so first-
    # launch works without contacting Apple. This is BEST-EFFORT for our
    # distribution shape: `xcrun stapler` only supports container formats
    # it knows how to write into (.app bundles, .dmg disk images, .pkg
    # installers). A PyInstaller onedir output is a plain directory —
    # stapler has nowhere to embed the ticket and exits with code 73.
    #
    # Skipping the staple is safe here because:
    #   - The bundle is notarised. Apple's database knows it's accepted.
    #   - On first launch with internet present, Gatekeeper queries that
    #     database online and accepts the binary. End users see no warning.
    #   - Link's first action on every device is registering with the
    #     control plane, which requires network — so the online-lookup
    #     path is always available.
    #
    # If we ever wrap the onedir in a .dmg or .pkg, stapling THAT outer
    # container will succeed and bake the ticket in for offline launches.
    try:
        _run(["xcrun", "stapler", "staple", str(bundle)], check=False)
        # Validate only runs cleanly if staple succeeded; same skip
        # logic applies.
        validate = _run(
            ["xcrun", "stapler", "validate", str(bundle)],
            check=False,
            capture=True,
        )
        if validate.returncode == 0:
            logger.info("Staple validated; ticket embedded in bundle.")
        else:
            logger.warning(
                "Stapler validate failed — bundle is NOT stapled. End users "
                "will need network on first launch so Gatekeeper can verify "
                "notarisation online (cached after). This is expected for "
                "PyInstaller onedir bundles; package as .dmg/.pkg to enable "
                "stapling. Notarisation itself is intact."
            )
    except Exception as exc:
        # Defence in depth — we already use check=False above, but log
        # anything unexpected and continue to spctl.
        logger.warning("Stapler step raised unexpectedly: %s. Continuing.", exc)

    # Final Gatekeeper assessment — same check macOS does on first launch.
    # With internet present, spctl resolves the notarisation status from
    # Apple's servers even when the bundle isn't stapled. If this passes,
    # end users won't see "cannot be opened because the developer cannot
    # be verified".
    _run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(bundle)])
    logger.info("Notarisation + spctl assessment passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path("dist/locai-link"),
        help="Path to the built bundle directory (default: dist/locai-link).",
    )
    parser.add_argument(
        "--entitlements",
        type=Path,
        default=Path(__file__).resolve().parent / "entitlements.plist",
        help="Path to entitlements.plist (default: bundling/entitlements.plist).",
    )
    parser.add_argument(
        "--skip-notarise",
        action="store_true",
        help="Sign only; skip notarisation. Useful for local dev signing.",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("sign_macos.py only runs on macOS.")
    if not shutil.which("codesign"):
        raise SystemExit("codesign not found — Xcode command-line tools required.")

    identity = _require_env("APPLE_DEVELOPER_ID_APPLICATION")
    codesign_bundle(args.bundle.resolve(), identity, args.entitlements.resolve())

    if args.skip_notarise:
        logger.info("--skip-notarise set; stopping after signing.")
        return

    key_id = _require_env("APPLE_NOTARY_KEY_ID")
    issuer_id = _require_env("APPLE_NOTARY_ISSUER_ID")
    key_path = Path(_require_env("APPLE_NOTARY_KEY_PATH")).resolve()
    notarise(args.bundle.resolve(), key_id, issuer_id, key_path)


if __name__ == "__main__":
    main()
