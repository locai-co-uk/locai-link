# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
# pyright: reportUndefinedVariable=false
# ruff: noqa: F821
# PyInstaller spec — produces a self-contained onedir bundle of Link.
# Plugin selection is driven by the LOCAI_BUNDLE_PLUGINS env var set by
# bundling/build.py (comma-separated plugin names).
#
# Note: `Analysis`, `PYZ`, `EXE`, `COLLECT`, and `SPECPATH` are injected into
# this file's exec namespace by PyInstaller at parse time — the suppressions
# above silence the resulting "undefined name" lint warnings.

import os
import platform as _pf
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parent


def _platform_tag() -> str:
    # Mirrors bundling/manifest.py::platform_tag (arch token x64/arm64). Kept
    # standalone: a PyInstaller .spec shouldn't import project modules. Unknown
    # architectures fail the build rather than silently selecting x64 natives.
    machine = _pf.machine().lower()
    arch_map = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}
    arch = arch_map.get(machine)
    if arch is None:
        raise SystemExit(f"unsupported architecture for bundling: {machine!r}")
    os_slug = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[_pf.system()]
    return f"{os_slug}-{arch}"


# Map plugin short-name → (pip dist-name, [hidden imports], native-bundle-dir or None).
# `native_dir` is the subdirectory under _artifacts/<platform>/ whose contents
# get copied into the bundle root for that plugin (None for pure-Python plugins).
PLUGIN_SPEC = {
    "language_model": {
        "dist": "link-language-model",
        "imports": [
            "link_language_model",
            "link_language_model.adapter",
            "link_language_model.server",
            "link_language_model.swap_manager",
            "link_language_model.install",
        ],
        "native_dir": "bin-llama",
    },
    "audio_transcriber": {
        "dist": "link-audio-transcriber",
        "imports": [
            "link_audio_transcriber",
            "link_audio_transcriber.adapter",
            "link_audio_transcriber.server",
            "link_audio_transcriber.install",
        ],
        "native_dir": "bin-whisper",
    },
    "audio_classifier": {
        "dist": "link-audio-classifier",
        "imports": ["link_audio_classifier", "link_audio_classifier.adapter"],
        "native_dir": None,
    },
    "image_classifier": {
        "dist": "link-image-classifier",
        "imports": ["link_image_classifier", "link_image_classifier.adapter"],
        "native_dir": None,
    },
}


# Empty is valid: a naked build (no --plugins) bundles just the runtime.
raw_selection = os.environ.get("LOCAI_BUNDLE_PLUGINS", "").strip()
selected = [p.strip() for p in raw_selection.split(",") if p.strip()]
unknown = [p for p in selected if p not in PLUGIN_SPEC]
if unknown:
    raise SystemExit(f"Unknown plugins in LOCAI_BUNDLE_PLUGINS: {', '.join(unknown)}")

# Engine binaries are baked in only for a --prefetch build; otherwise they are
# fetched from the artifact store on first use, so a selected plugin ships its
# Python code with no native binaries present.
prefetch = os.environ.get("LOCAI_BUNDLE_PREFETCH") == "1"
ARTIFACTS_DIR = SPEC_DIR / "_artifacts" / _platform_tag()

datas = []
binaries = []
hidden_imports: list[str] = []

for name in selected:
    info = PLUGIN_SPEC[name]
    # Dist-info — required for importlib.metadata entry-point discovery.
    try:
        datas += copy_metadata(info["dist"])
    except Exception as e:
        raise SystemExit(
            f"copy_metadata({info['dist']!r}) failed — make sure build.py "
            f"installed it into the active venv.\n  Underlying: {e}"
        )
    # Package data files (json, yaml etc. shipped beside the source).
    pkg = info["dist"].replace("-", "_")
    datas += collect_data_files(pkg, include_py_files=False)
    # Hidden imports — entry-point targets are loaded by string.
    hidden_imports += info["imports"]
    # Native binaries — baked in only for a --prefetch build (else fetched on demand).
    if info["native_dir"] and prefetch:
        native_root = ARTIFACTS_DIR / info["native_dir"]
        if not native_root.is_dir():
            raise SystemExit(
                f"Missing pre-fetched binaries at {native_root}. bundling/build.py should have prefetched them."
            )
        for entry in native_root.iterdir():
            if entry.is_file() or entry.is_symlink():
                datas.append((str(entry), info["native_dir"]))

# locai-link's own dist-info — utils/logger.py reads version() from it.
try:
    datas += copy_metadata("locai-link")
except Exception:
    pass  # editable installs of the root pkg may skip dist-info in some setups

# Default config templates.
for cfg in (REPO_ROOT / "configs").glob("*.json"):
    if cfg.name.endswith(".tmp.json"):
        continue
    datas.append((str(cfg), "configs"))


a = Analysis(
    [str(REPO_ROOT / "main.py")],
    pathex=[str(REPO_ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # Runtime binary lives inside versions/<v>/. The public-facing entry
    # point is the Rust launcher at <install_root>/locai-link, which exec's
    # this. Rename so the two don't collide when packaged into one install_root.
    name="locai-link-runtime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    # COLLECT name = output directory under dist/. Kept as "locai-link" so
    # PyInstaller writes to dist/locai-link/ and build.py's restructure step
    # finds it where it expects.
    name="locai-link",
)
