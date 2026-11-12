"""Regression contracts for the tracker display-window state machine.

The application currently ships its UI as one inline script. These focused
source-level assertions protect the ordering guarantees that cannot be covered
by the backend TestClient: scope hydration must happen before a paper request,
while the date-bar renderer must remain side-effect free.
"""

from __future__ import annotations

import re
from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def test_tracker_display_window_defaults_to_all_time_with_v2_storage() -> None:
    source = _source()
    reader = _section(
        source,
        "function _readDateWindowPreference()",
        "function _setTrackerDateWindowState(",
    )

    assert 'const TRACKER_DATE_WINDOW_STORAGE_PREFIX = "seismohub.dateWindow.v2.";' in source
    assert 'return `seismohub.dateWindow.${pid}`;' not in source
    assert "_defaultDateWindowForCurrentProject" not in source
    assert 'cad === "daily"' not in source
    assert 'cad === "weekly"' not in source
    assert 'cad === "monthly"' not in source
    assert 'if (!raw) return { kind: "all" };' in reader
    assert 'return { kind: "all" };' in reader


def test_legacy_values_are_ignored_and_v2_preferences_are_json() -> None:
    source = _source()
    key_builder = _section(source, "function _dateWindowStorageKey()", "function _isoDaysAgo(")
    reader = _section(
        source,
        "function _readDateWindowPreference()",
        "function _setTrackerDateWindowState(",
    )
    writer = _section(
        source,
        "function persistTrackerDateWindowPreference()",
        "// ─── PR-36: top-level nav state machine",
    )

    assert "TRACKER_DATE_WINDOW_STORAGE_PREFIX" in key_builder
    assert "JSON.parse(raw)" in reader
    assert "JSON.stringify(payload)" in writer
    assert "payload.dateFrom" in writer
    assert "payload.dateTo" in writer
    # The old namespace is documented for rollback, but is never read.
    assert 'localStorage.getItem("seismohub.dateWindow.' not in source


def test_scope_hydration_precedes_every_scope_change_request() -> None:
    source = _source()
    tab_switch = _section(source, "function setActiveAppTab(name)", "function setNavBadge(")
    workspace_switch = _section(source, "function switchWorkspace(projectId, folder)", "function renderBreadcrumb(")
    bootstrap = _section(source, "async function bootstrap()", "bootstrap().catch(")

    assert tab_switch.index("state.appTab = name;") < tab_switch.index("hydrateTrackerDateWindowState();")
    assert tab_switch.index("hydrateTrackerDateWindowState();") < tab_switch.index("loadActiveTab().catch")
    assert workspace_switch.index("hydrateTrackerDateWindowState();") < workspace_switch.index("loadPapers().catch")
    assert bootstrap.index("hydrateTrackerDateWindowState();") < bootstrap.index("await loadActiveTab();")


def test_collections_force_all_time_and_clear_tracker_dates() -> None:
    source = _source()
    setter = _section(
        source,
        "function _setTrackerDateWindowState(preference)",
        "function hydrateTrackerDateWindowState()",
    )
    tab_switch = _section(source, "function setActiveAppTab(name)", "function setNavBadge(")
    query_builder = _section(source, "function buildQuery()", "async function loadPapers()")

    assert 'const resolved = state.appTab === "trackers"' in setter
    assert ': { kind: "all" };' in setter
    assert 'state.filters.dateFrom = "";' in setter
    assert 'state.filters.dateTo = "";' in setter
    assert tab_switch.index("state.appTab = name;") < tab_switch.index("hydrateTrackerDateWindowState();")
    assert 'if (state.appTab === "trackers") {' in query_builder
    assert query_builder.index('if (state.appTab === "trackers") {') < query_builder.index('params.set("date_from"')


def test_date_bar_renderer_does_not_mutate_query_state() -> None:
    source = _source()
    renderer = _section(
        source,
        "function refreshTrackerDateBar()",
        "document.querySelectorAll('#trackerDateBar [data-window]')",
    )

    assert "localStorage." not in renderer
    assert "hydrateTrackerDateWindowState" not in renderer
    assert not re.search(r"state\.dateWindow\s*=(?!=)", renderer)
    assert not re.search(r"state\.filters\.dateFrom\s*=(?!=)", renderer)
    assert not re.search(r"state\.filters\.dateTo\s*=(?!=)", renderer)
    assert "loadPapers(" not in renderer
    assert "fetch(" not in renderer
    assert "classList.toggle" in renderer
    assert "textContent" in renderer
    assert "Showing ${from} onward" in renderer
    assert "Showing through ${to}" in renderer


def test_custom_date_edits_persist_before_refetch() -> None:
    source = _source()
    from_handler = _section(
        source,
        'dateFromFilter.addEventListener("change"',
        'dateToFilter.addEventListener("change"',
    )
    to_handler = _section(
        source,
        'dateToFilter.addEventListener("change"',
        "// ───────────────────────────────────────────────────────────────────",
    )

    for handler in (from_handler, to_handler):
        assert 'state.dateWindow === "custom"' in handler
        assert handler.index("persistTrackerDateWindowPreference();") < handler.index("await loadPapers();")


def test_explicit_window_is_applied_persisted_and_rendered_before_refetch() -> None:
    source = _source()
    apply_window = _section(
        source,
        "async function applyDateWindow(kind)",
        "function refreshTrackerDateBar()",
    )

    set_at = apply_window.index("_setTrackerDateWindowState(preference);")
    persist_at = apply_window.index("persistTrackerDateWindowPreference();")
    render_at = apply_window.index("refreshTrackerDateBar();")
    fetch_at = apply_window.index("await loadPapers();")
    assert set_at < persist_at < render_at < fetch_at
