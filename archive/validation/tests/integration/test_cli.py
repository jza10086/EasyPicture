from __future__ import annotations

import json
import os
import subprocess
import sys

from easypicture.persistence import PROJECT_SCHEMA_VERSION


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


def test_export_smoke_test_writes_all_static_formats(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "easypicture",
            "--export-smoke-test",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    assert set(diagnostics["formats"]) == {
        "png",
        "jpeg",
        "webp",
        "bmp",
        "tiff",
        "tga",
        "gif",
    }
    assert diagnostics["formats"]["gif"]["frames"] == 1
    assert diagnostics["sprite_frame"]["size"] == [4, 4]


def test_svg_smoke_test_round_trips_and_exports_both_modes(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "easypicture",
            "--svg-smoke-test",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    diagnostics = json.loads((tmp_path / "svg-diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["schema"] == PROJECT_SCHEMA_VERSION
    assert diagnostics["imported"] >= 2
    assert (tmp_path / "svg-smoke.limg").exists()
    assert (tmp_path / "svg-smoke-preserved.svg").exists()
    assert (tmp_path / "svg-smoke-rasterized.svg").exists()


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
    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["active_backend"] == "software"
    assert diagnostics["maximum_channel_error"] == 0
    assert (tmp_path / "framebuffer.png").exists()
    assert (tmp_path / "reference.png").exists()
