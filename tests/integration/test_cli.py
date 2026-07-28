from __future__ import annotations

import json
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


def test_software_render_smoke_outputs_reference_and_diagnostics(tmp_path) -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "easypicture",
            "--render-backend",
            "software",
            "--render-smoke-test",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    diagnostics = json.loads(
        (tmp_path / "diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["active_backend"] == "software"
    assert diagnostics["maximum_channel_error"] == 0
    assert (tmp_path / "framebuffer.png").exists()
    assert (tmp_path / "reference.png").exists()
