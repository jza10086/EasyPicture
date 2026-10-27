"""Isolated CLI/offscreen validation. No native pointer events or digest checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def smoke(prefix: list[str], output: Path, *, standalone: bool = True) -> list[dict]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / "temp"
    temporary.mkdir()
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM="offscreen", TEMP=str(temporary), TMP=str(temporary))
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if standalone:
        windows = Path(env.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    cases = {
        "app": ["--smoke-test"],
        "io": ["--io-smoke-test", str(output / "io")],
        "export": ["--export-smoke-test", str(output / "export")],
        "svg": ["--svg-smoke-test", str(output / "svg")],
        "software": [
            "--render-backend",
            "software",
            "--render-smoke-test",
            str(output / "software"),
        ],
    }
    results = []
    for name, args in cases.items():
        log = output / f"{name}.log"
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.run(
                [*prefix, *args],
                cwd=output,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        results.append({"case": name, "exit_code": process.returncode, "log": log.name})
        (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{name} smoke failed ({process.returncode}); see {log}")
        print(f"PASS {name}", flush=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--executable", type=Path)
    target.add_argument("--source", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    command = (
        [sys.executable, "-m", "easypicture"]
        if options.source
        else [str(options.executable.resolve())]
    )
    smoke(command, options.output, standalone=not options.source)
