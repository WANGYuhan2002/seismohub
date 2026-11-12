"""Tests for ``services/pdf_launcher.py`` (was 39 % coverage).

All subprocess invocations are mocked so the suite never spawns Skim /
Preview / xdg-open during CI. Coverage focuses on:
- ``_resolve_managed_path`` safety contract (rejects non-PDFs, non-files,
  unresolvable paths)
- ``enumerate_readers`` synthetic ``system_default`` insertion + cache TTL
- ``_enumerate_readers_darwin`` fallback when ``mdfind`` is absent
- ``launch_pdf`` argument validation (unknown reader_id rejected)
- ``launch_pdf`` happy path returns the ``{status, reader_id, ...}`` envelope
- Skim page-anchor URL (``skim://...#page=N``) construction
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import services.pdf_launcher as pdf_launcher
from services.pdf_launcher import (
    PdfLauncherError,
    PdfReader,
    _resolve_managed_path,
    enumerate_readers,
    launch_pdf,
)


# ----------------------------------------------------------------------
# Reset the module-level enum cache between tests
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_enum_cache():
    pdf_launcher._ENUM_CACHE["ts"] = 0.0
    pdf_launcher._ENUM_CACHE["readers"] = []
    yield
    pdf_launcher._ENUM_CACHE["ts"] = 0.0
    pdf_launcher._ENUM_CACHE["readers"] = []


# ----------------------------------------------------------------------
# _resolve_managed_path
# ----------------------------------------------------------------------


def test_resolve_managed_path_accepts_real_pdf(tmp_path):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    resolved = _resolve_managed_path(pdf)
    assert resolved == pdf.resolve()


def test_resolve_managed_path_rejects_missing_file(tmp_path):
    with pytest.raises(PdfLauncherError, match="could not be resolved"):
        _resolve_managed_path(tmp_path / "does-not-exist.pdf")


def test_resolve_managed_path_rejects_directory(tmp_path):
    """Strict mode means a directory is rejected as 'not a regular file'."""
    sub = tmp_path / "sub"
    sub.mkdir()
    # Path.resolve(strict=True) on a dir succeeds — so the next check
    # ('not is_file') fires the error path.
    with pytest.raises(PdfLauncherError, match="Not a regular file"):
        _resolve_managed_path(sub)


def test_resolve_managed_path_rejects_non_pdf_extension(tmp_path):
    txt = tmp_path / "fake.txt"
    txt.write_bytes(b"hello")
    with pytest.raises(PdfLauncherError, match="Not a .pdf"):
        _resolve_managed_path(txt)


def test_resolve_managed_path_accepts_uppercase_extension(tmp_path):
    pdf = tmp_path / "SAMPLE.PDF"
    pdf.write_bytes(b"%PDF-1.4")
    resolved = _resolve_managed_path(pdf)
    assert resolved == pdf.resolve()


# ----------------------------------------------------------------------
# PdfReader dataclass
# ----------------------------------------------------------------------


def test_pdf_reader_to_dict_round_trip():
    r = PdfReader(id="x", name="X Reader", platform="Darwin", path="/Apps/X.app")
    d = r.to_dict()
    assert d == {
        "id": "x",
        "name": "X Reader",
        "platform": "Darwin",
        "path": "/Apps/X.app",
        "icon_data_url": None,
    }


# ----------------------------------------------------------------------
# enumerate_readers — system_default insertion + caching
# ----------------------------------------------------------------------


def test_enumerate_readers_always_includes_system_default():
    """Whatever the per-platform enumerator returns, the result must
    contain a synthetic ``system_default`` reader at index 0."""
    readers = enumerate_readers(force_refresh=True)
    assert any(r.id == "system_default" for r in readers)
    assert readers[0].id == "system_default" or readers[0].name == "System default"


def test_enumerate_readers_caches_between_calls():
    """Second call within the TTL must not re-invoke the per-platform helper."""
    enumerate_readers(force_refresh=True)
    # Call again — should use cache
    with patch.object(pdf_launcher, "_enumerate_readers_darwin") as darwin_mock, \
         patch.object(pdf_launcher, "_enumerate_readers_linux") as linux_mock, \
         patch.object(pdf_launcher, "_enumerate_readers_windows") as windows_mock:
        readers = enumerate_readers(force_refresh=False)
        # None of the per-platform enumerators should have been called.
        darwin_mock.assert_not_called()
        linux_mock.assert_not_called()
        windows_mock.assert_not_called()
    assert any(r.id == "system_default" for r in readers)


def test_enumerate_readers_force_refresh_bypasses_cache():
    enumerate_readers(force_refresh=True)
    # Now stub out the platform enumerator to confirm force_refresh re-runs it
    with patch.object(
        pdf_launcher, f"_enumerate_readers_{platform.system().lower()}", return_value=[]
    ) as plat_mock:
        enumerate_readers(force_refresh=True)
        plat_mock.assert_called_once()


# ----------------------------------------------------------------------
# Per-platform enumerators (Darwin fallback path)
# ----------------------------------------------------------------------


def test_enumerate_readers_darwin_falls_back_to_hints_when_mdfind_missing():
    """When ``mdfind`` isn't on PATH, the Darwin enumerator returns the
    hard-coded ``_DARWIN_PDF_APP_HINTS`` table verbatim."""
    if platform.system() != "Darwin":
        pytest.skip("Darwin-only path")
    with patch("services.pdf_launcher.shutil.which", return_value=None):
        readers = pdf_launcher._enumerate_readers_darwin()
    assert readers, "should not be empty — every hint is added"
    assert any(r.id == "com.apple.Preview" for r in readers)
    assert all(r.platform == "Darwin" for r in readers)


def test_enumerate_readers_darwin_handles_mdfind_failure():
    """``subprocess.run`` raising an OSError should not propagate."""
    if platform.system() != "Darwin":
        pytest.skip("Darwin-only path")
    with patch("services.pdf_launcher.shutil.which", return_value="/usr/bin/mdfind"), \
         patch(
            "services.pdf_launcher.subprocess.run",
            side_effect=OSError("simulated failure"),
        ):
        readers = pdf_launcher._enumerate_readers_darwin()
    # Hints still backfill even when mdfind crashes
    assert any(r.id == "com.apple.Preview" for r in readers)


def test_enumerate_readers_linux_returns_empty_when_no_apps_dir(tmp_path):
    """A host with no /usr/share/applications / ~/.local/share/applications
    should yield an empty list (the system_default sentinel is added by
    the outer ``enumerate_readers``)."""
    with patch.object(pdf_launcher, "_LINUX_APPS_DIRS", [tmp_path / "no-such-dir"]):
        out = pdf_launcher._enumerate_readers_linux()
    assert out == []


def test_enumerate_readers_linux_picks_up_pdf_desktop_files(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "evince.desktop").write_text(
        "[Desktop Entry]\nName=Evince\nMimeType=application/pdf;\nExec=evince %u\n",
        encoding="utf-8",
    )
    (apps / "browser.desktop").write_text(
        "[Desktop Entry]\nName=Browser\nMimeType=text/html;\nExec=browser %u\n",
        encoding="utf-8",
    )
    with patch.object(pdf_launcher, "_LINUX_APPS_DIRS", [apps]):
        out = pdf_launcher._enumerate_readers_linux()
    ids = {r.id for r in out}
    assert "evince" in ids
    assert "browser" not in ids  # text/html mime not application/pdf


# ----------------------------------------------------------------------
# launch_pdf — argument validation
# ----------------------------------------------------------------------


def test_launch_pdf_rejects_unknown_reader_id(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    # Prime the enumeration cache with a known set so "not-a-real-reader"
    # cannot accidentally collide with a real bundle ID.
    pdf_launcher._ENUM_CACHE["ts"] = 9e9  # never expire
    pdf_launcher._ENUM_CACHE["readers"] = [
        PdfReader(id="system_default", name="System default", platform="Darwin"),
        PdfReader(id="com.apple.Preview", name="Preview", platform="Darwin"),
    ]
    with pytest.raises(PdfLauncherError, match="Unknown reader_id"):
        launch_pdf(pdf, reader_id="not-a-real-reader")


def test_launch_pdf_propagates_path_resolution_failure(tmp_path):
    """If the PDF path can't be resolved we want a PdfLauncherError, not
    a raw OSError/ValueError bubbling out."""
    with pytest.raises(PdfLauncherError):
        launch_pdf(tmp_path / "ghost.pdf")


def test_launch_pdf_system_default_id_is_accepted(tmp_path):
    """``system_default`` must NOT be validated against enumerate_readers
    (it's the synthetic fallback)."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch.object(pdf_launcher, "_spawn_detached") as spawn_mock:
        result = launch_pdf(pdf, reader_id="system_default")
    assert result["status"] == "ok"
    spawn_mock.assert_called_once()


# ----------------------------------------------------------------------
# launch_pdf — happy paths (mocked subprocess)
# ----------------------------------------------------------------------


def test_launch_pdf_darwin_skim_uses_url_scheme_with_page(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("Darwin-only behaviour")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    # Prime cache so the skim bundle id is "known"
    pdf_launcher._ENUM_CACHE["ts"] = 9e9
    pdf_launcher._ENUM_CACHE["readers"] = [
        PdfReader(id="net.sourceforge.skim-app.skim", name="Skim", platform="Darwin"),
        PdfReader(id="system_default", name="System default", platform="Darwin"),
    ]
    with patch.object(pdf_launcher, "_spawn_detached") as spawn_mock:
        result = launch_pdf(pdf, reader_id="net.sourceforge.skim-app.skim", page=42)
    assert result["status"] == "ok"
    assert result["page"] == 42
    spawn_mock.assert_called_once()
    argv = spawn_mock.call_args[0][0]
    assert argv[0] == "open"
    assert any("skim://" in arg and "#page=42" in arg for arg in argv)


def test_launch_pdf_darwin_default_uses_open_command(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("Darwin-only behaviour")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch.object(pdf_launcher, "_spawn_detached") as spawn_mock:
        result = launch_pdf(pdf)  # no reader_id → OS default
    assert result["status"] == "ok"
    assert result["reader_id"] == "system_default"
    argv = spawn_mock.call_args[0][0]
    assert argv == ["open", str(pdf.resolve())]


def test_launch_pdf_darwin_with_bundle_id_uses_open_b(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("Darwin-only behaviour")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    pdf_launcher._ENUM_CACHE["ts"] = 9e9
    pdf_launcher._ENUM_CACHE["readers"] = [
        PdfReader(id="com.adobe.Reader", name="Adobe Reader", platform="Darwin"),
        PdfReader(id="system_default", name="System default", platform="Darwin"),
    ]
    with patch.object(pdf_launcher, "_spawn_detached") as spawn_mock:
        result = launch_pdf(pdf, reader_id="com.adobe.Reader")
    assert result["status"] == "ok"
    assert result["reader_id"] == "com.adobe.Reader"
    argv = spawn_mock.call_args[0][0]
    assert argv == ["open", "-b", "com.adobe.Reader", str(pdf.resolve())]


def test_launch_pdf_linux_uses_xdg_open_when_no_reader(tmp_path):
    if platform.system() != "Linux":
        pytest.skip("Linux-only behaviour")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch("services.pdf_launcher.shutil.which", return_value="/usr/bin/xdg-open"), \
         patch.object(pdf_launcher, "_spawn_detached") as spawn_mock:
        result = launch_pdf(pdf)
    assert result["status"] == "ok"
    spawn_mock.assert_called_once()
    argv = spawn_mock.call_args[0][0]
    assert argv[0] == "xdg-open"


def test_launch_pdf_linux_raises_when_no_xdg_or_gtk(tmp_path):
    if platform.system() != "Linux":
        pytest.skip("Linux-only behaviour")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch("services.pdf_launcher.shutil.which", return_value=None):
        with pytest.raises(PdfLauncherError, match="Neither gtk-launch nor xdg-open"):
            launch_pdf(pdf)


# ----------------------------------------------------------------------
# Unsupported platform branch
# ----------------------------------------------------------------------


def test_launch_pdf_unsupported_platform_raises(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch.object(pdf_launcher, "_PLATFORM", "Plan9"):
        with pytest.raises(PdfLauncherError, match="Unsupported platform"):
            launch_pdf(pdf)
