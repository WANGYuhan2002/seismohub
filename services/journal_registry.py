"""Unified journal registry — the single source of truth for v2 (BRIEF v2 §4).

Before this module the journal universe was scattered across three places that
drifted independently:

* ``services/fetch_service.py`` — ``JOURNALS_BY_ISSN`` (the OpenAlex/Crossref
  intake list, keyed by ISSN) and ``RSS_SOURCES`` (the RSS intake list).
* ``services/journal_abbrev.py`` — ``_JOURNAL_ABBREVS_RAW`` (the title ->
  abbreviation/color map that drives the sidebar pip and the
  ``/api/journals/canonical`` picker).

BRIEF v2 §4 collapses all of that into ONE list of ``Journal`` dicts here. The
old modules now *derive* their public surface from this registry instead of
hand-maintaining parallel literals, so adding a journal is a one-line edit and
the three views can no longer disagree.

Schema of each entry (a plain ``dict`` — kept JSON-y so it round-trips through
``/api/journals/canonical`` and is greppable):

    {
        "name":   str,            # canonical full title (the registry key)
        "abbrev": str,            # short label shown in the sidebar pip
        "color":  str,            # "#rrggbb" hint for the pip / row accent
        "issn":   list[str],      # print+electronic ISSNs (may be empty)
        "rss":    str | None,     # RSS feed URL, when one exists
        "tier":   "pure" | "broad",
        # Optional fetch-shape overrides — only present when the label that
        # fetch_service historically used for OpenAlex/Crossref ("fetch_name")
        # or RSS ("rss_name") differs from the canonical "name". They exist so
        # journals_by_issn()/rss_sources() reproduce fetch_service's old labels
        # byte-for-byte (the paper.journal column already stores them, and a
        # rename would orphan rows / break the abbrev lookup).
        "fetch_name": str,        # optional
        "rss_name":   str,        # optional
    }

Tier semantics (BRIEF v2 §5):

* ``"pure"``  — a dedicated seismology / geophysics venue.
* ``"broad"`` — a multidisciplinary venue (Nature/Science/PNAS/Sci Adv/
  Nat Comms and the GRL/JGR umbrella, plus cross-disciplinary titles like
  IEEE TGRS).

Both tiers now equally pass ``in_seismic_domain`` (2026-06-14); tier is
display/tracking only, no intake bypass.

This module is intentionally dependency-free (stdlib only) so importing it can
never drag in google-genai / numpy / network and stays safe for ``db.py`` and
pytest collection — exactly the contract ``services/cos_topics.py`` already
honours.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Dict, List, Optional

__all__ = [
    "JOURNALS",
    "all_journals",
    "journals_by_issn",
    "rss_sources",
    "abbrev_for",
    "color_for",
    "tier_for",
    "pure_journal_names",
    "broad_journal_names",
    "abbrev_color_map",
    "DEFAULT_COLOR",
    "PURE",
    "BROAD",
]


# Tier sentinels — string-typed so they serialise cleanly and read well in the
# registry literal below.
PURE = "pure"
BROAD = "broad"

# Fallback pip color for any name not found in the registry. Mirrors the
# slate-500 default that ``journal_abbrev.journal_color`` historically returned
# for unknown journals so the two modules stay visually consistent.
DEFAULT_COLOR = "#64748b"


# ---------------------------------------------------------------------------
# THE REGISTRY
# ---------------------------------------------------------------------------
#
# ~80 mainstream seismology / geophysics / Earth-science journals plus the
# cross-disciplinary venues that regularly carry seismology work. Ordered
# loosely by family (core seismology -> solid-earth geophysics -> exploration
# -> Copernicus open-access -> AGU umbrella -> EGU/AGU adjacent -> remote
# sensing / signal -> high-impact generalists -> preprints) for readability;
# code never relies on the order.
#
# Provenance of the literals:
#   * Every journal already in fetch_service.JOURNALS_BY_ISSN / RSS_SOURCES is
#     reproduced here with its exact ISSNs, RSS URL, and (where it differed
#     from the canonical title) its historical fetch/rss label.
#   * Every journal already in journal_abbrev._JOURNAL_ABBREVS_RAW keeps its
#     exact abbrev + color so the sidebar pip does not change.
#   * The BRIEF v2 §4 additions (Geophysical Prospecting, J. Seismology,
#     Reviews of Geophysics, Earth-Science Reviews, Communications Earth &
#     Environment, Solid Earth, NHESS, PEPI, PAGEOPH, Surveys in Geophysics,
#     Computers & Geosciences, IEEE TGRS, ...) round the list out to ~80.

JOURNALS: List[Dict[str, object]] = [
    # ----- Core seismology -------------------------------------------------
    {
        "name": "Bulletin of the Seismological Society of America",
        "abbrev": "BSSA",
        "color": "#7f1d1d",
        "issn": ["0037-1106", "1943-3573"],
        "rss": "https://pubs.geoscienceworld.org/rss/site_531/185.xml",
        "tier": PURE,
        "fetch_name": "BSSA",
        "rss_name": "BSSA",
    },
    {
        "name": "Seismological Research Letters",
        "abbrev": "SRL",
        "color": "#c2410c",
        "issn": ["0895-0695", "1938-2057"],
        "rss": "https://pubs.geoscienceworld.org/rss/site_532/186.xml",
        "tier": PURE,
    },
    {
        "name": "Geophysical Journal International",
        "abbrev": "GJI",
        "color": "#0f766e",
        "issn": ["0956-540X", "1365-246X"],
        "rss": "https://academic.oup.com/rss/site_5282/3148.xml",
        "tier": PURE,
        "fetch_name": "GJI",
        "rss_name": "GJI (Current Issue)",
    },
    {
        # GJI advance-access is the SAME journal as the current-issue feed but a
        # distinct RSS endpoint; fetch_service historically listed it as its own
        # RSS source and folded both labels back to "GJI" via
        # JOURNAL_ALIASES_TO_ISSN_CANON. We keep it as a registry row whose
        # canonical name still abbreviates to GJI, but with NO issn (the ISSN
        # intake belongs to the row above) so OpenAlex/Crossref are not double
        # scanned.
        "name": "GJI (Advance Access)",
        "abbrev": "GJI",
        "color": "#0f766e",
        "issn": [],
        "rss": "https://academic.oup.com/rss/site_5282/advanceAccess_3148.xml",
        "tier": PURE,
    },
    {
        "name": "Seismica",
        "abbrev": "Seismica",
        "color": "#7f1d1d",
        "issn": ["2816-9387"],
        "rss": "https://seismica.library.mcgill.ca/gateway/plugin/WebFeedGatewayPlugin/rss",
        "tier": PURE,
    },
    {
        "name": "Earthquake Spectra",
        "abbrev": "EQ Spectra",
        "color": "#7c2d12",
        "issn": ["8755-2930", "1944-8201"],
        "rss": "https://pubs.geoscienceworld.org/earthquake-spectra/issue/current?fmt=rss",
        "tier": PURE,
    },
    # ----- Solid-earth geophysics -----------------------------------------
    {
        "name": "Tectonophysics",
        "abbrev": "Tectonophys",
        "color": "#7c2d12",
        "issn": ["0040-1951"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Tectonics",
        "abbrev": "Tectonics",
        "color": "#7c2d12",
        "issn": ["0278-7407", "1944-9194"],
        "rss": "https://agupubs.onlinelibrary.wiley.com/feed/19449194/most-recent",
        "tier": PURE,
    },
    {
        "name": "Earth and Planetary Science Letters",
        "abbrev": "EPSL",
        "color": "#0f172a",
        "issn": ["0012-821X"],
        "rss": None,
        "tier": PURE,
        "fetch_name": "EPSL",
    },
    {
        "name": "Physics of the Earth and Planetary Interiors",
        "abbrev": "PEPI",
        "color": "#5b21b6",
        "issn": ["0031-9201"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Geochemistry, Geophysics, Geosystems",
        "abbrev": "G3",
        "color": "#365314",
        "issn": ["1525-2027"],
        "rss": "https://agupubs.onlinelibrary.wiley.com/feed/15252027/most-recent",
        "tier": PURE,
        "fetch_name": "G3",
        "rss_name": "G3",
    },
    {
        "name": "Pure and Applied Geophysics",
        "abbrev": "PAGEOPH",
        "color": "#0e7490",
        "issn": ["0033-4553", "1420-9136"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Journal of Volcanology and Geothermal Research",
        "abbrev": "JVGR",
        "color": "#9d174d",
        "issn": ["0377-0273"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Journal of Geophysics and Engineering",
        "abbrev": "J Geophys Eng",
        "color": "#1e40af",
        "issn": ["1742-2132", "1742-2140"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Earth, Planets and Space",
        "abbrev": "EPS",
        "color": "#0f172a",
        "issn": ["1880-5981"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Chinese Journal of Geophysics",
        "abbrev": "Chin J Geophys",
        "color": "#7f1d1d",
        "issn": ["0001-5733"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Earthquake Science",
        "abbrev": "EQ Sci",
        "color": "#7f1d1d",
        "issn": ["1674-4519", "1867-8777"],
        "rss": None,
        "tier": PURE,
    },
    # ----- Reviews / surveys ----------------------------------------------
    {
        "name": "Reviews of Geophysics",
        "abbrev": "Rev Geophys",
        "color": "#1e3a8a",
        "issn": ["8755-1209", "1944-9208"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Surveys in Geophysics",
        "abbrev": "Surv Geophys",
        "color": "#0e7490",
        "issn": ["0169-3298", "1573-0956"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Earth-Science Reviews",
        "abbrev": "Earth-Sci Rev",
        "color": "#0f172a",
        "issn": ["0012-8252"],
        "rss": None,
        "tier": PURE,
    },
    # ----- Exploration / applied / signal geophysics ----------------------
    {
        "name": "Geophysics",
        "abbrev": "Geophysics",
        "color": "#1e40af",
        "issn": ["0016-8033", "1942-2156"],
        "rss": "https://library.seg.org/action/showFeed?type=etoc&feed=rss&jc=gpysa7",
        "tier": PURE,
        "fetch_name": "Geophysics (SEG)",
        "rss_name": "Geophysics (SEG)",
    },
    {
        "name": "Geophysical Prospecting",
        "abbrev": "Geophys Prospect",
        "color": "#1e40af",
        "issn": ["0016-8025", "1365-2478"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Journal of Applied Geophysics",
        "abbrev": "J Appl Geophys",
        "color": "#1e40af",
        "issn": ["0926-9851"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Computers & Geosciences",
        "abbrev": "Comput Geosci",
        "color": "#365314",
        "issn": ["0098-3004"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Computational Geosciences",
        "abbrev": "Comput Geosci (CG)",
        "color": "#365314",
        "issn": ["1420-0597", "1573-1499"],
        "rss": None,
        "tier": PURE,
    },
    # ----- Copernicus / open-access Earth science -------------------------
    {
        "name": "Solid Earth",
        "abbrev": "Solid Earth",
        "color": "#0e7490",
        "issn": ["1869-9510", "1869-9529"],
        "rss": "https://se.copernicus.org/xml/rss1_0.xml",
        "tier": PURE,
    },
    {
        "name": "Natural Hazards and Earth System Sciences",
        "abbrev": "NHESS",
        "color": "#9a3412",
        "issn": ["1561-8633", "1684-9981"],
        "rss": "https://nhess.copernicus.org/xml/rss1_0.xml",
        "tier": PURE,
        "rss_name": "NHESS",
    },
    {
        "name": "Nonlinear Processes in Geophysics",
        "abbrev": "NPG",
        "color": "#5b21b6",
        "issn": ["1023-5809", "1607-7946"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Geoscientific Model Development",
        "abbrev": "GMD",
        "color": "#365314",
        "issn": ["1991-959X", "1991-9603"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Annales Geophysicae",
        "abbrev": "Ann Geophys (EGU)",
        "color": "#0f766e",
        "issn": ["0992-7689", "1432-0576"],
        "rss": None,
        "tier": PURE,
    },
    # ----- AGU umbrella (broad: JGR/GRL family carry off-topic too) -------
    {
        "name": "Geophysical Research Letters",
        "abbrev": "GRL",
        "color": "#1e3a8a",
        "issn": ["0094-8276", "1944-8007"],
        "rss": "https://agupubs.onlinelibrary.wiley.com/feed/19448007/most-recent",
        "tier": BROAD,
        "fetch_name": "GRL",
        "rss_name": "GRL",
    },
    {
        "name": "Journal of Geophysical Research: Solid Earth",
        "abbrev": "JGR-SE",
        "color": "#1e3a8a",
        "issn": ["2169-9313", "2169-9356"],
        "rss": "https://agupubs.onlinelibrary.wiley.com/feed/21699356/most-recent",
        "tier": BROAD,
        "fetch_name": "JGR: Solid Earth",
        "rss_name": "JGR: Solid Earth",
    },
    {
        "name": "Journal of Geophysical Research",
        "abbrev": "JGR",
        "color": "#1e3a8a",
        "issn": [],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Earth and Space Science",
        "abbrev": "ESS",
        "color": "#0f172a",
        "issn": ["2333-5084"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "AGU Advances",
        "abbrev": "AGU Adv",
        "color": "#1e3a8a",
        "issn": ["2576-604X"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Space Weather",
        "abbrev": "Space Weather",
        "color": "#1e3a8a",
        "issn": ["1542-7390"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Journal of Advances in Modeling Earth Systems",
        "abbrev": "JAMES",
        "color": "#365314",
        "issn": ["1942-2466"],
        "rss": None,
        "tier": BROAD,
    },
    # ----- Remote sensing / signal / instrumentation (cross-disciplinary) -
    {
        "name": "IEEE Transactions on Geoscience and Remote Sensing",
        "abbrev": "IEEE TGRS",
        "color": "#0e7490",
        "issn": ["0196-2892", "1558-0644"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "IEEE Geoscience and Remote Sensing Letters",
        "abbrev": "IEEE GRSL",
        "color": "#0e7490",
        "issn": ["1545-598X", "1558-0571"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "IEEE Transactions on Signal Processing",
        "abbrev": "IEEE TSP",
        "color": "#0e7490",
        "issn": ["1053-587X", "1941-0476"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Remote Sensing",
        "abbrev": "Remote Sens",
        "color": "#365314",
        "issn": ["2072-4292"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Sensors",
        "abbrev": "Sensors",
        "color": "#365314",
        "issn": ["1424-8220"],
        "rss": None,
        "tier": BROAD,
    },
    # ----- Adjacent Earth / environmental (broad domain filter) -----------
    {
        "name": "Geology",
        "abbrev": "Geology",
        "color": "#365314",
        "issn": ["0091-7613", "1943-2682"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Bulletin of Volcanology",
        "abbrev": "Bull Volcanol",
        "color": "#9d174d",
        "issn": ["0258-8900", "1432-0819"],
        "rss": None,
        "tier": PURE,
    },
    {
        "name": "Journal of Hydrology",
        "abbrev": "J Hydrol",
        "color": "#0e7490",
        "issn": ["0022-1694"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Water Resources Research",
        "abbrev": "WRR",
        "color": "#1e3a8a",
        "issn": ["0043-1397", "1944-7973"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "The Cryosphere",
        "abbrev": "The Cryosphere",
        "color": "#0e7490",
        "issn": ["1994-0416", "1994-0424"],
        "rss": None,
        "tier": BROAD,
    },
    # ----- High-impact multidisciplinary (broad domain filter) ------------
    {
        "name": "Nature",
        "abbrev": "Nature",
        "color": "#000000",
        "issn": ["0028-0836", "1476-4687"],
        "rss": "https://www.nature.com/nature.rss",
        "tier": BROAD,
    },
    {
        "name": "Nature Geoscience",
        "abbrev": "Nat Geosci",
        "color": "#111827",
        "issn": ["1752-0894", "1752-0908"],
        "rss": "https://www.nature.com/ngeo/current_issue/rss/",
        "tier": BROAD,
    },
    {
        "name": "Nature Communications",
        "abbrev": "Nat Comms",
        "color": "#111827",
        "issn": ["2041-1723"],
        "rss": "https://www.nature.com/ncomms.rss",
        "tier": BROAD,
    },
    {
        "name": "Communications Earth & Environment",
        "abbrev": "Comms Earth Env",
        "color": "#111827",
        "issn": ["2662-4435"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Nature Reviews Earth & Environment",
        "abbrev": "Nat Rev EE",
        "color": "#111827",
        "issn": ["2662-138X"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Scientific Reports",
        "abbrev": "Sci Rep",
        "color": "#0e7490",
        "issn": ["2045-2322"],
        "rss": "https://www.nature.com/srep.rss",
        "tier": BROAD,
    },
    {
        # Nature's aggregated "Seismology" subject feed — RSS-only, no ISSN.
        "name": "Nature Subject: Seismology (aggregated feed)",
        "abbrev": "Nature Seismology",
        "color": "#111827",
        "issn": [],
        "rss": "https://www.nature.com/subjects/seismology.rss",
        "tier": BROAD,
    },
    {
        "name": "Science",
        "abbrev": "Science",
        "color": "#7f1d1d",
        "issn": ["0036-8075", "1095-9203"],
        "rss": "https://www.science.org/rss/science/current",
        "tier": BROAD,
    },
    {
        "name": "Science Advances",
        "abbrev": "Sci Adv",
        "color": "#7f1d1d",
        "issn": ["2375-2548"],
        "rss": "https://www.science.org/rss/sciadv/current",
        "tier": BROAD,
    },
    {
        "name": "Proceedings of the National Academy of Sciences",
        "abbrev": "PNAS",
        "color": "#1d4ed8",
        "issn": ["0027-8424", "1091-6490"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "Science Bulletin",
        "abbrev": "Sci Bull",
        "color": "#7f1d1d",
        "issn": ["2095-9273", "2095-9281"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "National Science Review",
        "abbrev": "Natl Sci Rev",
        "color": "#1d4ed8",
        "issn": ["2095-5138", "2053-714X"],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "PLOS ONE",
        "abbrev": "PLOS ONE",
        "color": "#0e7490",
        "issn": ["1932-6203"],
        "rss": None,
        "tier": BROAD,
    },
    # ----- Preprint servers ------------------------------------------------
    {
        "name": "arXiv",
        "abbrev": "arXiv",
        "color": "#92400e",
        "issn": [],
        "rss": None,
        "tier": BROAD,
    },
    {
        "name": "EarthArXiv",
        "abbrev": "EarthArXiv",
        "color": "#92400e",
        "issn": [],
        "rss": None,
        # 2026-06-14: BROAD so topic-wide EarthArXiv intake is seismology-gated
        # (it carries all earth science). Watchlist-author preprints still arrive
        # via explicit_follow, which bypasses the gate.
        "tier": BROAD,
    },
]

# ── 2026-06-14 owner preset review: add missing venues + cull low-quality ────
# Applied at import, right after the literal, so every accessor below (each
# iterates the module-global JOURNALS) sees the final augmented + filtered set.
JOURNALS += [
    {"name": "The Seismic Record", "abbrev": "TSR", "color": "#7f1d1d", "issn": ["2694-4006"], "rss": None, "tier": PURE},
    {"name": "Journal of Geophysical Research: Machine Learning and Computation", "abbrev": "JGR-ML", "color": "#1e3a8a", "issn": ["2993-5210"], "rss": None, "tier": BROAD, "fetch_name": "JGR: Machine Learning and Computation"},
    {"name": "Journal of Geophysical Research: Earth Surface", "abbrev": "JGR-ES", "color": "#1e3a8a", "issn": ["2169-9003", "2169-9011"], "rss": None, "tier": BROAD, "fetch_name": "JGR: Earth Surface"},
    {"name": "The Leading Edge", "abbrev": "TLE", "color": "#1e40af", "issn": ["1070-485X", "1938-3789"], "rss": None, "tier": BROAD, "fetch_name": "The Leading Edge"},
    {"name": "ESS Open Archive", "abbrev": "ESSOAr", "color": "#92400e", "issn": [], "rss": None, "tier": BROAD, "fetch_name": "ESS Open Archive"},
]

# Journals the owner culled in the 2026-06-14 preset review — removed from the
# active registry so they are never fetched-by-ISSN nor admitted as a 'pure'
# masthead. (Papers are ALSO hard-rejected at intake by CULLED_JOURNAL_NAMES in
# fetch_service.passes_domain_gate, and existing rows were deleted from the DB.)
CULLED_JOURNAL_NAMES = {
    "Earthquake Spectra",
    "Earth-Science Reviews",
    "Pure and Applied Geophysics",
    "Tectonics",
    "Physics of the Earth and Planetary Interiors",
    "Journal of Geophysics and Engineering",
    "Earth, Planets and Space",
    "Surveys in Geophysics",
    "Journal of Applied Geophysics",
    "Computers & Geosciences",
    "Computational Geosciences",
    "Natural Hazards and Earth System Sciences",
    "Nonlinear Processes in Geophysics",
    "Geoscientific Model Development",
    "Annales Geophysicae",
    "Earth and Space Science",
    "Remote Sensing",
    "Geology",
    "Bulletin of Volcanology",
    "Journal of Hydrology",
    "Water Resources Research",
    "PLOS ONE",
    # Not in the registry above (arrived via "Others"/broad), listed so the
    # fetch_service blacklist can import one authoritative set:
    "NHESS",
    "KTH Publication Database DiVA (KTH Royal Institute of Technology)",
    "The Journal of the Acoustical Society of America",
    "Glacies",
    "Processes",
    "Open MIND",
    # 2026-07-10 owner cull: EGU General Assembly / Galileo Conference abstracts
    # (Copernicus Meetings) arrive via the OpenAlex topic trackers as conference
    # abstracts — no full paper, low signal. Hard-reject at intake.
    "EGU General Assembly",
}
JOURNALS = [entry for entry in JOURNALS if str(entry["name"]) not in CULLED_JOURNAL_NAMES]


# ---------------------------------------------------------------------------
# Name canonicaliser (shared shape with journal_abbrev._normalize)
# ---------------------------------------------------------------------------
#
# We keep a *local* normaliser so lookups in this module are independent of the
# abbrev module (journal_abbrev imports FROM us, never the reverse). The regex /
# accent-folding is intentionally BYTE-IDENTICAL to journal_abbrev._normalize so
# a title resolves to the same key in both places — this is what lets
# journal_abbrev.abbreviate() find the registry-derived rows. (Note the '&' is
# deliberately NOT in the punctuation class, matching journal_abbrev: an
# ampersand survives as its own token, e.g. "Communications Earth & Environment"
# -> "communications earth & environment" on BOTH sides.)

_PUNCT_RE = re.compile(r"[\s\-_.,:;/\\()\[\]{}'\"!?]+")


def _normalize(value: Optional[str]) -> str:
    """Lowercase + strip accents + collapse punctuation to spaces."""
    s = unicodedata.normalize("NFKD", value or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def _index_by_name() -> Dict[str, Dict[str, object]]:
    """Map normalized canonical title -> the registry entry.

    Built once and cached. If two rows normalize to the same key (e.g. the GJI
    current-issue and advance-access rows both abbreviate to GJI but have
    distinct names, so they DON'T collide) the first wins — order in JOURNALS is
    therefore the tie-break, matching the "first occurrence" rule the legacy
    ``/api/journals/canonical`` endpoint already used.
    """
    index: Dict[str, Dict[str, object]] = {}
    for entry in JOURNALS:
        key = _normalize(str(entry["name"]))
        if key and key not in index:
            index[key] = entry
    return index


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def all_journals() -> List[Dict[str, object]]:
    """Return the full registry as a fresh list of shallow-copied dicts.

    Copies so a caller mutating an entry (e.g. tagging UI state) cannot corrupt
    the module-level source of truth.
    """
    return [dict(entry) for entry in JOURNALS]


def journals_by_issn() -> List[Dict[str, object]]:
    """Intake list for OpenAlex/Crossref, in fetch_service's exact shape.

    Returns ``[{"journal": str, "issn": [str, ...]}, ...]`` — one row per
    registry entry, using the historical ``fetch_name`` label when present so
    ``paper.journal`` values stay byte-identical to the pre-registry pipeline.
    Entries with an empty ISSN list are still included (fetch_service already
    skips empty-ISSN rows itself), preserving the old list 1:1.
    """
    out: List[Dict[str, object]] = []
    for entry in JOURNALS:
        label = str(entry.get("fetch_name") or entry["name"])
        issns = list(entry.get("issn") or [])
        out.append({"journal": label, "issn": issns})
    return out


def rss_sources() -> List[Dict[str, str]]:
    """Intake list for the RSS path, in fetch_service's exact shape.

    Returns ``[{"journal": str, "rss": str}, ...]`` for every registry entry
    that declares an ``rss`` URL, using the historical ``rss_name`` label when
    present (e.g. "GJI (Current Issue)", "Geophysics (SEG)").
    """
    out: List[Dict[str, str]] = []
    for entry in JOURNALS:
        rss = entry.get("rss")
        if not rss:
            continue
        label = str(entry.get("rss_name") or entry["name"])
        out.append({"journal": label, "rss": str(rss)})
    return out


def abbrev_color_map() -> Dict[str, tuple]:
    """Return ``{normalized_name: (abbrev, color)}`` for the abbrev module.

    Includes BOTH the canonical title key AND a self-referencing key for the
    abbreviation itself (so e.g. a paper whose ``journal`` column already stores
    "GJI" resolves). This is the single hook ``services/journal_abbrev.py`` uses
    to derive its table from the registry. Title keys win over abbrev keys on
    collision (the title is the more specific source).
    """
    table: Dict[str, tuple] = {}
    # First pass: abbreviation self-refs (lower priority).
    for entry in JOURNALS:
        abbrev = str(entry["abbrev"])
        color = str(entry["color"])
        abbrev_key = _normalize(abbrev)
        if abbrev_key:
            table.setdefault(abbrev_key, (abbrev, color))
    # Second pass: canonical titles (override any colliding abbrev self-ref).
    for entry in JOURNALS:
        name_key = _normalize(str(entry["name"]))
        if name_key:
            table[name_key] = (str(entry["abbrev"]), str(entry["color"]))
    return table


def _lookup(name: Optional[str]) -> Optional[Dict[str, object]]:
    return _index_by_name().get(_normalize(name))


def abbrev_for(name: Optional[str]) -> Optional[str]:
    """Return the registry abbreviation for a canonical title, or ``None``.

    Only resolves exact (normalized) registry titles — the fuzzy ISO-4 fallback
    for unknown journals lives in ``journal_abbrev.abbreviate`` and is NOT
    duplicated here.
    """
    entry = _lookup(name)
    return str(entry["abbrev"]) if entry else None


def color_for(name: Optional[str]) -> str:
    """Return the registry pip color for a title, or ``DEFAULT_COLOR``."""
    entry = _lookup(name)
    return str(entry["color"]) if entry else DEFAULT_COLOR


def tier_for(name: Optional[str]) -> Optional[str]:
    """Return ``"pure"`` / ``"broad"`` for a title, or ``None`` if unknown.

    Tier is used for display/tracking; the intake gate treats all venues
    uniformly (see ``fetch_service.passes_domain_gate``) — every venue must pass
    ``in_seismic_domain`` and tier grants no bypass. Returning ``None`` lets the
    caller decide the default for an unknown venue.
    """
    entry = _lookup(name)
    return str(entry["tier"]) if entry else None


def pure_journal_names() -> List[str]:
    """Canonical titles of every ``tier="pure"`` journal."""
    return [str(e["name"]) for e in JOURNALS if e.get("tier") == PURE]


def broad_journal_names() -> List[str]:
    """Canonical titles of every ``tier="broad"`` journal."""
    return [str(e["name"]) for e in JOURNALS if e.get("tier") == BROAD]
