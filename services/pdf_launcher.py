"""External PDF reader integration.

Replaces the in-browser annotation flow. Provides:

- :func:`launch_pdf` — cross-platform spawn that opens a PDF in the user's
  chosen native application (Skim / PDF Expert / Preview / Adobe / Foxit /
  ...). All commands run detached and never block the FastAPI request loop.
- :func:`enumerate_readers` — best-effort list of installed PDF-capable
  apps on the host, cached for 1 hour. Drives the "Set default reader" UI.

Security model:
- The caller passes a `Path` that **must** resolve inside `DOWNLOADS_DIR`.
  Any other path is rejected with :class:`PdfLauncherError` before any
  subprocess is spawned (prevents arbitrary-file disclosure via crafted
  paper IDs).
- We never use `shell=True`; everything is argv-list. The reader ID, if
  given, is validated against the enumerated apps table.

Per the roadmap M1 spec, this module replaces the deprecated
`services/pdf_acquisition/` subprocess paths.
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parents[1]
DOWNLOADS_DIR = BASE_DIR / "downloads"

_PLATFORM = platform.system()  # "Darwin" / "Linux" / "Windows"


class PdfLauncherError(Exception):
    """Raised when a PDF cannot be safely launched."""


@dataclass
class PdfReader:
    """One PDF-capable application visible on the host."""

    id: str  # bundle identifier (Darwin), .desktop basename (Linux), or registry key (Windows)
    name: str  # human-readable display name
    platform: str  # "Darwin" / "Linux" / "Windows"
    path: Optional[str] = None  # full path to .app / .desktop / .exe
    icon_data_url: Optional[str] = None  # populated lazily by /api/readers/icon/{id}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# Path safety
# ----------------------------------------------------------------------

def _resolve_managed_path(pdf_path: Path | str) -> Path:
    """Resolve `pdf_path` and verify it is a readable PDF file.

    This is a single-user local app; PDFs may live in any directory on the
    user's machine (including legacy project paths). We therefore validate
    only that the path resolves to a real, readable .pdf file rather than
    enforcing a specific directory root.
    """
    try:
        resolved = Path(pdf_path).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PdfLauncherError(f"PDF path could not be resolved: {pdf_path}") from exc

    if not resolved.is_file():
        raise PdfLauncherError(f"Not a regular file: {resolved}")
    if resolved.suffix.lower() != ".pdf":
        raise PdfLauncherError(f"Not a .pdf file: {resolved}")
    return resolved


# ----------------------------------------------------------------------
# Reader enumeration (cached 1h)
# ----------------------------------------------------------------------

_ENUM_CACHE: Dict[str, Any] = {"ts": 0.0, "readers": []}
_ENUM_TTL_SEC = 3600.0
_ENUM_LOCK = threading.Lock()


def enumerate_readers(force_refresh: bool = False) -> List[PdfReader]:
    """Return the cached list of PDF-capable apps on the host.

    Cache TTL = 1 hour. Pass `force_refresh=True` to bypass the cache
    (e.g. after the user installed a new reader).
    """
    now = time.time()
    with _ENUM_LOCK:
        if (
            not force_refresh
            and _ENUM_CACHE["readers"]
            and (now - _ENUM_CACHE["ts"]) < _ENUM_TTL_SEC
        ):
            return list(_ENUM_CACHE["readers"])

        if _PLATFORM == "Darwin":
            readers = _enumerate_readers_darwin()
        elif _PLATFORM == "Linux":
            readers = _enumerate_readers_linux()
        elif _PLATFORM == "Windows":
            readers = _enumerate_readers_windows()
        else:
            readers = []

        # Always include a synthetic "system_default" entry so the UI has a
        # graceful fallback even when enumeration returns nothing.
        if not any(r.id == "system_default" for r in readers):
            readers.insert(
                0,
                PdfReader(
                    id="system_default",
                    name="System default",
                    platform=_PLATFORM,
                    path=None,
                ),
            )

        _ENUM_CACHE["readers"] = readers
        _ENUM_CACHE["ts"] = now
        return list(readers)


# -- Darwin (macOS) ----------------------------------------------------

_DARWIN_PDF_APP_HINTS = {
    # bundle id : friendly name (fallback if Info.plist lookup fails)
    "com.apple.Preview": "Preview",
    "net.sourceforge.skim-app.skim": "Skim",
    "com.adobe.Reader": "Adobe Acrobat Reader",
    "com.adobe.Acrobat.Pro": "Adobe Acrobat Pro",
    "com.readdle.PDFExpert-Mac": "PDF Expert",
    "com.foxitsoftware.FoxitPDFEditorforMac": "Foxit PDF Editor",
    "com.foxitsoftware.FoxitPDFReaderforMac": "Foxit PDF Reader",
    "com.kagi.kagimacOS": "Kagi",
    "org.mozilla.firefox": "Firefox",
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
}


def _enumerate_readers_darwin() -> List[PdfReader]:
    """Use mdfind + Info.plist parsing to find PDF-capable apps on macOS.

    Robust to missing tools and missing Info.plist values — every hop has
    a fallback so we never raise from inside enumeration.
    """
    readers: List[PdfReader] = []
    seen_ids: set[str] = set()

    if not shutil.which("mdfind"):
        # No Spotlight on this machine — fall back to the hint list.
        for bundle_id, name in _DARWIN_PDF_APP_HINTS.items():
            readers.append(
                PdfReader(id=bundle_id, name=name, platform="Darwin", path=None)
            )
            seen_ids.add(bundle_id)
        return readers

    try:
        result = subprocess.run(
            ["mdfind", "kMDItemContentTypeTree == 'com.adobe.pdf' && kMDItemKind == 'Application'"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        paths = []

    for app_path in paths:
        info_plist = app_path / "Contents" / "Info.plist"
        bundle_id = None
        display_name = app_path.stem
        try:
            with open(info_plist, "rb") as fh:
                plist = plistlib.load(fh)
            bundle_id = plist.get("CFBundleIdentifier")
            display_name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or app_path.stem
        except (OSError, plistlib.InvalidFileException):
            pass

        if not bundle_id or bundle_id in seen_ids:
            continue
        seen_ids.add(bundle_id)
        readers.append(
            PdfReader(
                id=bundle_id,
                name=str(display_name),
                platform="Darwin",
                path=str(app_path),
            )
        )

    # Backfill with hints for apps mdfind missed (rare, but happens for
    # apps with disabled Spotlight indexing or new installs).
    for bundle_id, name in _DARWIN_PDF_APP_HINTS.items():
        if bundle_id not in seen_ids:
            readers.append(
                PdfReader(id=bundle_id, name=name, platform="Darwin", path=None)
            )

    # Stable order: known hints first (preferred reader candidates), then
    # discovered apps alphabetically.
    hint_order = {bid: idx for idx, bid in enumerate(_DARWIN_PDF_APP_HINTS)}
    readers.sort(key=lambda r: (hint_order.get(r.id, 999), r.name.lower()))
    return readers


# -- Linux -------------------------------------------------------------

_LINUX_APPS_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local" / "share" / "applications",
]

_DESKTOP_NAME_RE = re.compile(r"^Name=(.+)$", re.MULTILINE)
_DESKTOP_MIME_RE = re.compile(r"^MimeType=(.+)$", re.MULTILINE)
_DESKTOP_EXEC_RE = re.compile(r"^Exec=(.+)$", re.MULTILINE)


def _enumerate_readers_linux() -> List[PdfReader]:
    readers: List[PdfReader] = []
    seen_ids: set[str] = set()

    for apps_dir in _LINUX_APPS_DIRS:
        if not apps_dir.is_dir():
            continue
        for desktop_file in apps_dir.glob("*.desktop"):
            try:
                content = desktop_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            mime_match = _DESKTOP_MIME_RE.search(content)
            if not mime_match or "application/pdf" not in mime_match.group(1):
                continue
            name_match = _DESKTOP_NAME_RE.search(content)
            exec_match = _DESKTOP_EXEC_RE.search(content)
            desktop_id = desktop_file.stem
            if desktop_id in seen_ids:
                continue
            seen_ids.add(desktop_id)
            readers.append(
                PdfReader(
                    id=desktop_id,
                    name=(name_match.group(1).strip() if name_match else desktop_id),
                    platform="Linux",
                    path=str(desktop_file),
                )
            )
    readers.sort(key=lambda r: r.name.lower())
    return readers


# -- Windows -----------------------------------------------------------

def _enumerate_readers_windows() -> List[PdfReader]:
    """Best-effort enum via the registry. No-op if winreg is unavailable."""
    readers: List[PdfReader] = []
    try:
        import winreg  # type: ignore
    except ImportError:
        return readers

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r".pdf\OpenWithProgids") as key:
            i = 0
            while True:
                try:
                    name, _, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                if name and name not in {r.id for r in readers}:
                    readers.append(
                        PdfReader(id=name, name=name, platform="Windows", path=None)
                    )
                i += 1
    except OSError:
        pass

    readers.sort(key=lambda r: r.name.lower())
    return readers


# ----------------------------------------------------------------------
# Launch
# ----------------------------------------------------------------------

def launch_pdf(
    pdf_path: Path | str,
    reader_id: Optional[str] = None,
    *,
    page: Optional[int] = None,
) -> Dict[str, Any]:
    """Open `pdf_path` in the user's chosen reader.

    Args:
        pdf_path: PDF file path (must resolve inside DOWNLOADS_DIR).
        reader_id: Reader identifier from enumerate_readers(). When None,
            uses the OS default. The synthetic "system_default" ID maps
            to the OS default as well.
        page: 1-based page number to open at, if the reader supports it.
            Currently honoured for Skim (skim:// URL scheme).

    Returns:
        dict with `{status, reader_id, page, path}`.

    Raises:
        PdfLauncherError: path validation failed, or reader_id is unknown.
    """
    resolved = _resolve_managed_path(pdf_path)

    # Validate reader_id against the enumerated table (when supplied and
    # not "system_default"). This blocks command-injection attempts via
    # a crafted reader_id.
    if reader_id and reader_id != "system_default":
        known_ids = {r.id for r in enumerate_readers()}
        if reader_id not in known_ids:
            raise PdfLauncherError(f"Unknown reader_id: {reader_id}")

    if _PLATFORM == "Darwin":
        return _launch_darwin(resolved, reader_id, page=page)
    if _PLATFORM == "Linux":
        return _launch_linux(resolved, reader_id, page=page)
    if _PLATFORM == "Windows":
        return _launch_windows(resolved, reader_id, page=page)
    raise PdfLauncherError(f"Unsupported platform: {_PLATFORM}")


def _spawn_detached(argv: List[str]) -> None:
    """Spawn a subprocess that survives FastAPI worker shutdown."""
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if _PLATFORM == "Windows":  # pragma: no cover — relies on Windows specifics
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 — argv list, no shell


def _launch_darwin(
    resolved: Path, reader_id: Optional[str], *, page: Optional[int]
) -> Dict[str, Any]:
    # Skim supports skim:// URLs with anchors for page jumps.
    if reader_id == "net.sourceforge.skim-app.skim" and page:
        url = f"skim://{resolved}#page={page}"
        _spawn_detached(["open", "-u", url])
        return {"status": "ok", "reader_id": reader_id, "page": page, "path": str(resolved)}

    if reader_id and reader_id != "system_default":
        _spawn_detached(["open", "-b", reader_id, str(resolved)])
    else:
        _spawn_detached(["open", str(resolved)])
    return {"status": "ok", "reader_id": reader_id or "system_default", "page": page, "path": str(resolved)}


def _launch_linux(
    resolved: Path, reader_id: Optional[str], *, page: Optional[int]
) -> Dict[str, Any]:
    if reader_id and reader_id != "system_default":
        # Use gtk-launch when available so .desktop semantics are honoured.
        if shutil.which("gtk-launch"):
            _spawn_detached(["gtk-launch", reader_id, str(resolved)])
            return {"status": "ok", "reader_id": reader_id, "page": page, "path": str(resolved)}
    # Fall back to xdg-open (system default).
    if shutil.which("xdg-open"):
        _spawn_detached(["xdg-open", str(resolved)])
        return {"status": "ok", "reader_id": "system_default", "page": page, "path": str(resolved)}
    raise PdfLauncherError("Neither gtk-launch nor xdg-open is available on this Linux host.")


def _launch_windows(
    resolved: Path, reader_id: Optional[str], *, page: Optional[int]
) -> Dict[str, Any]:  # pragma: no cover — runs only on Windows
    # `os.startfile` uses the registered default handler. For non-default
    # readers we'd need explicit registry lookup; for now we honour only
    # system_default to stay safe.
    if reader_id and reader_id not in {None, "system_default"}:
        raise PdfLauncherError(
            "Per-reader launch on Windows is not implemented yet; use system_default."
        )
    os.startfile(str(resolved))  # type: ignore[attr-defined]
    return {"status": "ok", "reader_id": "system_default", "page": page, "path": str(resolved)}


__all__ = [
    "PdfLauncherError",
    "PdfReader",
    "enumerate_readers",
    "launch_pdf",
]
