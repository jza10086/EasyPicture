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
