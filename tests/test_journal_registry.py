"""Unified journal registry contract tests (BRIEF v2 §4).

``services/journal_registry.py`` is the single source of truth from which
``fetch_service.JOURNALS_BY_ISSN`` / ``RSS_SOURCES`` and the
``journal_abbrev`` table all derive. These tests pin the invariants the brief
requires of that registry:

* ~45 mainstream seismology / geophysics journals (BRIEF v2 §4 originally
  asked for ~80; owner cull 2026-06-14 reduced the active set to 45).
* EPSL (already present) AND the BRIEF v2 §4 additions (Geophysical
  Prospecting, J. Seismology, Reviews of Geophysics, Earth-Science Reviews,
  Communications Earth & Environment, Solid Earth, NHESS, PEPI, PAGEOPH,
  Surveys in Geophysics, Computers & Geosciences, IEEE TGRS) all in the list
  with the right ISSNs.
* Each entry carries a ``tier`` of exactly ``"pure"`` or ``"broad"``.
* The derived intake lists (``journals_by_issn`` / ``rss_sources``) have the
  exact ``{"journal", "issn"}`` / ``{"journal", "rss"}`` shapes fetch_service
  iterates, and the abbrev map is non-empty and resolves a known title.

The module is stdlib-only and import-safe, so these tests need no fixtures,
no DB, and no network.
"""

from __future__ import annotations

import re

import pytest

from services import journal_registry as reg


# ---------------------------------------------------------------------------
# Registry size + shape.
# ---------------------------------------------------------------------------


def test_registry_size_post_cull_2026_06_14():
    """BRIEF v2 §4 originally ~80; owner cull 2026-06-14 -> 45 active (see
    CULLED_JOURNAL_NAMES).

    Bound it loosely (42..48) so a one-line add/remove never trips the test
    while still catching an accidental truncation or a runaway duplication.
    """
    n = len(reg.JOURNALS)
    assert 42 <= n <= 48, f"expected ~45 journals post-cull, registry has {n}"


def test_every_entry_has_required_keys_and_types():
    """Each registry dict must carry the schema keys with sane types."""
    for entry in reg.JOURNALS:
        assert isinstance(entry, dict)
        name = entry.get("name")
        assert isinstance(name, str) and name.strip(), f"bad name: {entry!r}"

        abbrev = entry.get("abbrev")
        assert isinstance(abbrev, str) and abbrev.strip(), f"{name}: bad abbrev"

        color = entry.get("color")
        assert isinstance(color, str) and re.fullmatch(
            r"#[0-9a-fA-F]{6}", color
        ), f"{name}: color is not #rrggbb: {color!r}"

        issn = entry.get("issn")
        assert isinstance(issn, list), f"{name}: issn must be a list"
        for code in issn:
            assert isinstance(code, str) and re.fullmatch(
                r"\d{4}-\d{3}[\dXx]", code
            ), f"{name}: malformed ISSN {code!r}"

        rss = entry.get("rss")
        assert rss is None or (isinstance(rss, str) and rss.strip()), (
            f"{name}: rss must be a non-empty str or None"
        )

        tier = entry.get("tier")
        assert tier in (reg.PURE, reg.BROAD), (
            f"{name}: tier must be 'pure' or 'broad', got {tier!r}"
        )


def test_tier_values_are_exactly_pure_or_broad():
    """No third tier ever sneaks in (tier is display/tracking only; kept to exactly pure/broad)."""
    tiers = {str(e["tier"]) for e in reg.JOURNALS}
    assert tiers <= {"pure", "broad"}, f"unexpected tier(s): {tiers}"
    # Both tiers must be represented — a registry that lost one tier would
    # silently break tier-based display/labeling.
    assert "pure" in tiers
    assert "broad" in tiers


def test_canonical_names_are_unique():
    """Two rows must not share a canonical title (the abbrev/tier index keys
    on the normalized name; a collision would shadow one row)."""
    names = [str(e["name"]) for e in reg.JOURNALS]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate canonical journal names: {sorted(dupes)}"


# ---------------------------------------------------------------------------
# Specific journals the brief calls out by name.
# ---------------------------------------------------------------------------


def _entry_by_name(name: str):
    key = reg._normalize(name)
    for entry in reg.JOURNALS:
        if reg._normalize(str(entry["name"])) == key:
            return entry
    return None


def test_epsl_present_with_issn_and_pure_tier():
    """EPSL was already in the registry; confirm it survived the refactor."""
    entry = _entry_by_name("Earth and Planetary Science Letters")
    assert entry is not None, "EPSL missing from registry"
    assert "0012-821X" in entry["issn"]
    assert entry["tier"] == reg.PURE
    # Its historical fetch label is EPSL — used by journals_by_issn().
    assert reg.abbrev_for("Earth and Planetary Science Letters") == "EPSL"


def test_geophysical_prospecting_added_with_both_issns():
    """BRIEF v2 §4 explicitly adds Geophysical Prospecting (0016-8025 /
    1365-2478)."""
    entry = _entry_by_name("Geophysical Prospecting")
    assert entry is not None, "Geophysical Prospecting missing from registry"
    assert "0016-8025" in entry["issn"]
    assert "1365-2478" in entry["issn"]
    assert entry["tier"] in (reg.PURE, reg.BROAD)


@pytest.mark.parametrize(
    "title",
    [
        # Owner cull 2026-06-14 removed 6 of the original §4 additions
        # (see test_culled_journals_absent). "Journal of Seismology" was
        # considered but the owner decided NOT to adopt it (owner 2026-06-15),
        # so it is intentionally absent from this list too.
        "Reviews of Geophysics",
        "Communications Earth & Environment",
        "Solid Earth",
        "IEEE Transactions on Geoscience and Remote Sensing",
    ],
)
def test_brief_v2_section4_additions_present(title: str):
    """The BRIEF v2 §4 additions that survived the 2026-06-14 cull must still
    be in the registry."""
    assert _entry_by_name(title) is not None, f"{title!r} missing from registry"


@pytest.mark.parametrize(
    "title",
    [
        "Earth-Science Reviews",
        "Natural Hazards and Earth System Sciences",
        "Physics of the Earth and Planetary Interiors",
        "Pure and Applied Geophysics",
        "Surveys in Geophysics",
        "Computers & Geosciences",
    ],
)
def test_culled_journals_absent(title: str):
    """The 6 journals the owner culled 2026-06-14 must be recorded in
    CULLED_JOURNAL_NAMES and absent from the active registry."""
    assert title in reg.CULLED_JOURNAL_NAMES, (
        f"{title!r} expected in CULLED_JOURNAL_NAMES"
    )
    assert _entry_by_name(title) is None, f"{title!r} should be absent from registry"


def test_multidisciplinary_generalists_are_broad():
    """Nature/Science/PNAS/Sci Adv/Nat Comms and the GRL/JGR umbrella must be
    tagged broad so the §5 domain gate filters them (not take-all)."""
    broad_titles = [
        "Nature",
        "Science",
        "Science Advances",
        "Nature Communications",
        "Proceedings of the National Academy of Sciences",
        "Geophysical Research Letters",
        "Journal of Geophysical Research: Solid Earth",
    ]
    for title in broad_titles:
        assert reg.tier_for(title) == reg.BROAD, f"{title} should be tier=broad"


def test_dedicated_seismology_venues_are_pure():
    """Core seismology venues must be tier=pure (tier is a display label; intake gates all venues uniformly)."""
    pure_titles = [
        "Bulletin of the Seismological Society of America",
        "Seismological Research Letters",
        "Geophysical Journal International",
        "Earth and Planetary Science Letters",
    ]
    for title in pure_titles:
        assert reg.tier_for(title) == reg.PURE, f"{title} should be tier=pure"


# ---------------------------------------------------------------------------
# Derived intake lists — exact fetch_service shapes.
# ---------------------------------------------------------------------------


def test_journals_by_issn_shape_and_labels():
    """``journals_by_issn`` returns one ``{"journal", "issn"}`` row per entry,
    using the historical ``fetch_name`` label when present."""
    rows = reg.journals_by_issn()
    assert isinstance(rows, list) and rows
    assert len(rows) == len(reg.JOURNALS), "one row per registry entry expected"
    for row in rows:
        assert set(row.keys()) == {"journal", "issn"}, row
        assert isinstance(row["journal"], str) and row["journal"].strip()
        assert isinstance(row["issn"], list)

    labels = {r["journal"] for r in rows}
    # fetch_name override is surfaced (e.g. EPSL, BSSA, GJI, GRL).
    assert "EPSL" in labels
    assert "BSSA" in labels
    assert "GRL" in labels


def test_rss_sources_shape_only_includes_feeds():
    """``rss_sources`` returns ``{"journal", "rss"}`` rows ONLY for entries
    that declare an rss URL, with the historical rss_name label."""
    rows = reg.rss_sources()
    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row.keys()) == {"journal", "rss"}, row
        assert isinstance(row["rss"], str) and row["rss"].startswith("http")

    # Count must equal the number of registry entries with a truthy rss field.
    expected = sum(1 for e in reg.JOURNALS if e.get("rss"))
    assert len(rows) == expected

    labels = {r["journal"] for r in rows}
    # rss_name overrides are surfaced.
    assert "GJI (Current Issue)" in labels
    assert "Geophysics (SEG)" in labels


def test_abbrev_color_map_resolves_titles_and_abbrevs():
    """The abbrev map (consumed by journal_abbrev) is non-empty and resolves
    both a canonical title and its abbreviation to ``(abbrev, color)``."""
    table = reg.abbrev_color_map()
    assert isinstance(table, dict) and table

    title_key = reg._normalize("Bulletin of the Seismological Society of America")
    assert title_key in table
    abbrev, color = table[title_key]
    assert abbrev == "BSSA"
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", color)

    # The abbreviation self-ref resolves too (a paper.journal stored as "BSSA").
    assert reg._normalize("BSSA") in table


def test_pure_and_broad_name_lists_partition_the_registry():
    """``pure_journal_names`` + ``broad_journal_names`` together cover every
    journal exactly once."""
    pure = set(reg.pure_journal_names())
    broad = set(reg.broad_journal_names())
    assert pure and broad
    assert not (pure & broad), "a journal cannot be both pure and broad"
    all_names = {str(e["name"]) for e in reg.JOURNALS}
    assert pure | broad == all_names


def test_accessors_handle_unknown_titles_gracefully():
    """Lookups for an unknown journal must degrade, not raise."""
    assert reg.abbrev_for("Totally Made Up Journal of Nothing") is None
    assert reg.tier_for("Totally Made Up Journal of Nothing") is None
    assert reg.color_for("Totally Made Up Journal of Nothing") == reg.DEFAULT_COLOR
    # None input is tolerated.
    assert reg.tier_for(None) is None
    assert reg.color_for(None) == reg.DEFAULT_COLOR


def test_all_journals_returns_independent_copies():
    """``all_journals`` must hand back copies so a caller mutating a row can't
    corrupt the module-level source of truth."""
    snapshot = reg.all_journals()
    assert len(snapshot) == len(reg.JOURNALS)
    snapshot[0]["name"] = "MUTATED"
    assert reg.JOURNALS[0]["name"] != "MUTATED"
