from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from easypicture import __version__
from easypicture.render import CanvasRenderDiagnostics, RenderBackendKind
from easypicture.runtime import (
    LOG_BACKUP_COUNT,
    RuntimePaths,
    configure_logging,
    package_versions,
)
from easypicture.temp_files import (
    OWNERSHIP_MARKER,
    OWNERSHIP_SIGNATURE,
    TemporaryFileManager,
    cleanup_stale_sibling_temps,
)
from easypicture.ui.about_dialog import build_diagnostics_report
from easypicture.ui.notifications import NoticeSeverity, NotificationService
from tests.fakes import make_settings


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        app_data=tmp_path / "data",
        cache=tmp_path / "cache",
        logs=tmp_path / "data" / "logs",
        temp=tmp_path / "cache" / "temp",
    )


def test_rotating_log_is_utf8_and_uses_bounded_backups(tmp_path, monkeypatch) -> None:
    import easypicture.runtime as runtime

    monkeypatch.setattr(runtime, "LOG_MAX_BYTES", 256)
    path = configure_logging(_runtime_paths(tmp_path))
    logger = logging.getLogger("easypicture")
    for index in range(30):
        logger.info("本机日志轮转测试 %s %s", index, "x" * 30)
    for handler in logger.handlers:
        handler.flush()

    assert path.exists()
    assert path.with_name(f"{path.name}.1").exists()
    assert sum(1 for _ in path.parent.glob(f"{path.name}.*")) <= LOG_BACKUP_COUNT
    assert "EasyPicture" not in path.read_text(encoding="utf-8")


def test_stale_cleanup_only_removes_owned_entries(tmp_path) -> None:
    root = tmp_path / "owned-temp"
    manager = TemporaryFileManager(root)
    stale = manager.create_job_directory("stale-")
    foreign = root / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    old = time.time() - 48 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(foreign, (old, old))

    restarted = TemporaryFileManager(root)
    report = restarted.cleanup_stale(max_age_seconds=24 * 60 * 60)

    assert report.removed_entries == 1
    assert not stale.exists()
    assert foreign.exists()
    assert (root / OWNERSHIP_MARKER).read_text(encoding="utf-8") == OWNERSHIP_SIGNATURE


def test_active_temporary_directory_is_not_removed(tmp_path) -> None:
    manager = TemporaryFileManager(tmp_path / "temp")
    active = manager.create_job_directory("active-")
    old = time.time() - 48 * 60 * 60
    os.utime(active, (old, old))

    assert manager.cleanup_stale().removed_entries == 0
    assert active.exists()


def test_sibling_cleanup_matches_only_exact_destination(tmp_path) -> None:
    destination = tmp_path / "picture.png"
    stale = tmp_path / ".picture.png.deadbeef.tmp"
    unrelated = tmp_path / ".other.png.deadbeef.tmp"
    stale.write_bytes(b"stale")
    unrelated.write_bytes(b"keep")
    old = time.time() - 48 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))

    report = cleanup_stale_sibling_temps(destination)

    assert report.removed_entries == 1
    assert not stale.exists()
    assert unrelated.exists()


def test_notification_service_deduplicates_by_key(qtbot) -> None:
    service = NotificationService(dedupe_seconds=60)
    received: list[object] = []
    service.notice.connect(received.append)

    assert service.warning("same", "第一次")
    assert not service.warning("same", "重复")
    assert service.publish("other", NoticeSeverity.INFO, "其他")
    assert len(received) == 2


def test_recent_files_are_mru_deduplicated_and_bounded(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    paths = [tmp_path / f"{index}.limg" for index in range(12)]
    for path in paths:
        settings.add_recent_file(path)
    settings.add_recent_file(paths[5])

    recent = settings.recent_files()

    assert len(recent) == 10
    assert recent[0] == paths[5]
    assert len({str(path).casefold() for path in recent}) == len(recent)

    settings.remove_recent_file(paths[5])
    assert paths[5] not in settings.recent_files()
    settings.clear_recent_files()
    assert settings.recent_files() == ()


def test_about_report_contains_version_backend_and_local_paths(tmp_path) -> None:
    paths = _runtime_paths(tmp_path)
    diagnostics = CanvasRenderDiagnostics(
        requested_backend=RenderBackendKind.AUTO,
        active_backend=RenderBackendKind.OPENGL,
        vendor="Example GPU Vendor",
        renderer="Example GPU",
        version="4.6",
    )

    report = build_diagnostics_report(paths, diagnostics).text

    assert f"EasyPicture: {__version__}" in report
    assert "渲染后端: opengl" in report
    assert "Example GPU" in report
    assert str(paths.logs) in report


def test_about_report_does_not_call_uninitialized_opengl_unavailable(tmp_path) -> None:
    diagnostics = CanvasRenderDiagnostics(
        requested_backend=RenderBackendKind.AUTO,
        active_backend=RenderBackendKind.SOFTWARE,
        renderer="Qt QPainter",
    )

    report = build_diagnostics_report(_runtime_paths(tmp_path), diagnostics).text

    assert "渲染后端: OpenGL（等待画布初始化）" in report
    assert "OpenGL: 等待画布初始化（请求 3.3 Core）" in report
    assert "OpenGL: 不可用" not in report


def test_runtime_dependency_versions_do_not_require_distribution_metadata(
    monkeypatch,
) -> None:
    from importlib import metadata

    def missing_metadata(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing_metadata)

    versions = package_versions()

    for dependency in ("PySide6", "NumPy", "Pillow", "OpenCV"):
        assert versions[dependency] != "不可用"
        assert versions[dependency]
