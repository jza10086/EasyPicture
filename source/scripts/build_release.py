"""Compile and validate one EXE; keep intermediates and replaced releases in archive."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from importlib import metadata
from pathlib import Path

from release_smoke import smoke

SOURCE = Path(__file__).resolve().parents[1]
ROOT = SOURCE.parent


def inside(path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError(f"Path outside workspace: {path}")
    return path


def collect_licenses(destination: Path) -> None:
    destination.mkdir()
    packages = (
        "numpy",
        "opencv-python-headless",
        "Pillow",
        "PySide6",
        "PySide6_Essentials",
        "PySide6_Addons",
        "shiboken6",
        "Nuitka",
    )
    versions = {}
    for name in packages:
        distribution = metadata.distribution(name)
        versions[name] = distribution.version
        for file in distribution.files or ():
            if any(
                term in str(file).lower() for term in ("license", "copying", "copyright", "notice")
            ):
                source = Path(distribution.locate_file(file))
                if not source.is_file() or source.suffix.lower() in (".py", ".pyc", ".pyd", ".dll"):
                    continue
                relative = Path(str(file))
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                target = destination / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", destination / "Python-LICENSE.txt")
    (destination / "dependencies.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run = inside(ROOT / "archive" / "runs" / f"package-{stamp}")
    run.mkdir(parents=True)
    licenses = run / "licenses"
    collect_licenses(licenses)
    from easypicture.version import __version__

    output = run / "compiler"
    args = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=onefile",
        "--python-flag=-m",
        "--enable-plugin=pyside6",
        "--msvc=latest",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--output-filename=EasyPicture.exe",
        f"--output-dir={output}",
        f"--report={run / 'compilation.xml'}",
        f"--include-data-dir={licenses}=licenses",
        "--company-name=EasyPicture contributors",
        "--product-name=EasyPicture",
        f"--product-version={__version__}",
        f"--file-version={__version__}",
        "--file-description=EasyPicture image editor",
        str(SOURCE / "src" / "easypicture"),
    ]
    env = os.environ.copy()
    env["NUITKA_CACHE_DIR"] = str(SOURCE / ".cache" / "nuitka")
    (run / "command.json").write_text(json.dumps(args, indent=2), encoding="utf-8")
    print(f"Build log: {run / 'compiler.log'}", flush=True)
    with (run / "compiler.log").open("w", encoding="utf-8") as log:
        subprocess.run(args, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    candidate = output / "EasyPicture.exe"
    if not candidate.is_file():
        raise RuntimeError(f"Compiler did not produce {candidate}")
    staged = run / "distribution"
    staged.mkdir()
    shutil.copy2(candidate, staged / "EasyPicture.exe")
    results = smoke([str(staged / "EasyPicture.exe")], run / "smoke")
    release = inside(ROOT / "release")
    release.mkdir(exist_ok=True)
    existing = list(release.iterdir())
    if any(item.name != "EasyPicture.exe" or not item.is_file() for item in existing):
        raise RuntimeError("Unexpected files in release; archive them before publishing")
    if existing:
        old_release = inside(ROOT / "archive" / "releases" / stamp)
        old_release.mkdir(parents=True)
        for item in existing:
            shutil.copy2(inside(item), inside(old_release / item.name))
    # A failed replace (e.g. a running EXE locked by Windows) leaves the old release usable.
    (staged / "EasyPicture.exe").replace(release / "EasyPicture.exe")
    report = {
        "version": __version__,
        "executable": "release/EasyPicture.exe",
        "bytes": (release / "EasyPicture.exe").stat().st_size,
        "validation": results,
        "native_mouse": "manual acceptance pending",
        "hardware_opengl": "not covered by offscreen checks",
    }
    (run / "release.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Release: {release / 'EasyPicture.exe'}", flush=True)


if __name__ == "__main__":
    main()
