from __future__ import annotations

import os
import subprocess
import sys


def test_module_smoke_test_exits_successfully() -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-m", "easypicture", "--smoke-test"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


def test_io_smoke_test_round_trips_project_and_png(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "easypicture",
            "--io-smoke-test",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "io-smoke.limg").exists()
    assert (tmp_path / "io-smoke.png").exists()
