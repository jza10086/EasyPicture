from __future__ import annotations

from easypicture.ui.numeric_input_harness import NumericInputHarness


def test_isolated_numeric_input_state_switching_and_blocking(qtbot) -> None:
    harness = NumericInputHarness()
    qtbot.addWidget(harness)
    harness.show()

    reports = harness.run_automatic_checks()

    assert all(report.passed for report in reports), reports
    assert all(probe.block_failures == 0 for probe in harness._probes)  # noqa: SLF001
