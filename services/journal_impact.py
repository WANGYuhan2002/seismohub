"""Journal impact-factor sort (``sort_by=impact_factor``), the same order as SeismoHub Lite's "Journal IF" sort.

Order, as one ascending tuple compare (``impact_factor_sort_key``):

  group 0  journals with an impact factor in ``JOURNALS``, highest impact factor first
  group 1  other journals (no impact factor listed); the "Others" placeholder and papers without a journal last
  group 2  preprint servers, repositories, theses, datasets and meetings (``MISC_VENUE``)

Each journal stays together, and its papers run newest first. The spellings of one journal (``GJI`` and
``Geophysical Journal International``, ``JGR: Solid Earth`` and ``Journal of Geophysical Research Solid Earth``, ...)
share one journal key, so their papers form one block.

``JOURNALS`` and ``MISC_VENUE`` are copies of the tables in SeismoHub Lite's ``docs/index.html``, and
``LITE_ALIASES`` is Lite's ``JOURNAL_ALIASES`` (``tools/export_lite.py``): Lite renames those raw names before it
looks them up, and the app stores the raw names, so both forms resolve here. Update both projects together. The
impact factors are approximate Clarivate JCR values; like Lite, the app sorts by them and never shows them.

Dates follow Lite too: a year-only date ("2026") goes after the full dates of that year, and a paper without a
date goes last (the app's ``_neg_date`` would put both first).

Two small differences from Lite: journals with the same group and impact factor are ordered by name (Lite puts
the one with more papers first), and lookups also treat "&" as "and" and ignore a leading "The".
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

from services.journal_abbrev import canonicalize
from services.sort_keys import _invert_iso

# Journal name -> (short name, impact factor). 0 means no impact factor is listed.
JOURNALS: Dict[str, Tuple[str, float]] = {
    "Nature Reviews Earth & Environment": ("Nat Rev EE", 49.7),
    "Nature": ("Nature", 48.5),
    "Science": ("Science", 45.8),
    "Reviews of Geophysics": ("Rev Geophys", 38.5),
    "Science Bulletin": ("Sci Bull", 21.1),
    "National Science Review": ("Natl Sci Rev", 17.1),
    "Nature Geoscience": ("Nat Geosci", 16.1),
    "Nature Communications": ("Nat Comms", 15.7),
    "Nature Astronomy": ("Nat Astron", 14.3),
    "Science Advances": ("Sci Adv", 12.5),
    "Earth System Science Data": ("ESSD", 11.6),
    "Reliability Engineering & System Safety": ("RESS", 11.0),
    "Earth-Science Reviews": ("Earth-Sci Rev", 10.0),
    "Proceedings of the National Academy of Sciences": ("PNAS", 9.1),
    "Communications Earth & Environment": ("Comms Earth Env", 8.9),
    "Geoscience Frontiers": ("Geosci Front", 8.9),
    "IEEE Transactions on Geoscience and Remote Sensing": ("IEEE TGRS", 8.6),
    "Engineering Geology": ("Eng Geol", 8.4),
    "AGU Advances": ("AGU Adv", 7.4),
    "Journal of Building Engineering": ("J Build Eng", 7.4),
    "Journal of Hydrology": ("J Hydrol", 6.3),
    "Computers and Geotechnics": ("Comput Geotech", 6.2),
    "IEEE Transactions on Signal Processing": ("IEEE TSP", 5.8),
    "Geology": ("Geology", 5.4),
    "The Seismic Record": ("TSR", 5.3),
    "International Journal of Greenhouse Gas Control": ("IJGGC", 5.2),
    "GRL": ("GRL", 5.0), "Geophysical Research Letters": ("GRL", 5.0),
    "Water Resources Research": ("WRR", 5.0),
    "Surveys in Geophysics": ("Surv Geophys", 4.9),
    "Geoscientific Model Development": ("GMD", 4.9),
    "EPSL": ("EPSL", 4.8), "Earth and Planetary Science Letters": ("EPSL", 4.8),
    "The European Physical Journal C": ("EPJ C", 4.8),
    "Natural Hazards and Earth System Sciences": ("NHESS", 4.6),
    "The Cryosphere": ("Cryosphere", 4.6),
    "Earthquake Engineering & Structural Dynamics": ("EESD", 4.6),
    "Journal of Advances in Modeling Earth Systems": ("JAMES", 4.6),
    "International Journal of Disaster Risk Reduction": ("IJDRR", 4.5),
    "IEEE Geoscience and Remote Sensing Letters": ("IEEE GRSL", 4.4),
    "Computers & Geosciences": ("Comput Geosci", 4.2),
    "Ultrasonics": ("Ultrasonics", 4.1),
    "Remote Sensing": ("Remote Sens", 4.1),
    "JGR: Solid Earth": ("JGR-SE", 4.0), "Journal of Geophysical Research: Solid Earth": ("JGR-SE", 4.0),
    "Scientific Reports": ("Sci Rep", 3.9),
    "Space Weather": ("Space Weather", 3.8),
    "JGR: Planets": ("JGR-P", 3.7),
    "JGR: Earth Surface": ("JGR-ES", 3.6),
    "Tectonics": ("Tectonics", 3.5),
    "Sensors": ("Sensors", 3.5),
    "Physics in Medicine and Biology": ("Phys Med Biol", 3.5),
    "Earthquake Spectra": ("EQ Spectra", 3.4),
    "Seismological Research Letters": ("SRL", 3.2),
    "Geophysics (SEG)": ("Geophysics", 3.1), "Geophysics": ("Geophysics", 3.1),
    "G3": ("G3", 3.0), "Geochemistry, Geophysics, Geosystems": ("G3", 3.0),
    "Solid Earth": ("Solid Earth", 3.0),
    "Bulletin of Volcanology": ("Bull Volcanol", 3.0),
    "Earth Surface Processes and Landforms": ("ESPL", 3.0),
    "BSSA": ("BSSA", 2.9), "Bulletin of the Seismological Society of America": ("BSSA", 2.9),
    "Icarus": ("Icarus", 2.9),
    "Earth and Space Science": ("ESS", 2.9),
    "GJI": ("GJI", 2.8), "Geophysical Journal International": ("GJI", 2.8),
    "Earth Planets and Space": ("EPS", 2.8),
    "Tectonophysics": ("Tectonophys", 2.7),
    "PLOS ONE": ("PLOS ONE", 2.6),
    "Journal of Volcanology and Geothermal Research": ("JVGR", 2.4),
    "Physics of the Earth and Planetary Interiors": ("PEPI", 2.4),
    "The Journal of the Acoustical Society of America": ("JASA", 2.3),
    "Acta Geophysica": ("Acta Geophys", 2.1),
    "Journal of Applied Geophysics": ("J Appl Geophys", 2.1),
    "Computational Geosciences": ("Comput Geosci", 2.1),
    "Geophysical Prospecting": ("Geophys Prospect", 2.0),
    "Pure and Applied Geophysics": ("PAGEOPH", 1.9),
    "Nonlinear Processes in Geophysics": ("NPG", 1.7),
    "Annales Geophysicae": ("Ann Geophys", 1.7),
    "Journal of Seismology": ("J Seismol", 1.6),
    "Journal of Geophysics and Engineering": ("J Geophys Eng", 1.6),
    "Netherlands Journal of Geosciences – Geologie en Mijnbouw": ("Neth J Geosci", 1.4),
    "International Journal of Physical Modelling in Geotechnics": ("IJPMG", 1.4),
    "Earthquake Science": ("EQ Sci", 1.2),
    "The Leading Edge": ("TLE", 1.1),
    "Chinese Journal of Geophysics": ("Chin J Geophys", 1.0),
    "Seismica": ("Seismica", 0.0),
    "JGR: Machine Learning and Computation": ("JGR-ML", 0.0),
    "Journal of Geophysical Research": ("JGR", 0.0),
    "Nature Subject: Seismology (aggregated feed)": ("Nature Seismology", 0.0),
    "Proceedings of the International Ocean Discovery Program. Expedition reports": ("Proc IODP", 0.0),
    "arXiv": ("arXiv", 0.0), "ESS Open Archive": ("ESSOAr", 0.0), "EarthArXiv": ("EarthArXiv", 0.0), "Research Square": ("Res Square", 0.0),
    "Zenodo": ("Zenodo", 0.0), "Figshare": ("Figshare", 0.0), "HAL": ("HAL", 0.0), "OSTI": ("OSTI", 0.0), "WorkflowHub": ("WorkflowHub", 0.0),
    "EGU General Assembly": ("EGU GA", 0.0), "Earth System Science Data Discussions": ("ESSD Disc", 0.0), "Copernicus": ("Copernicus", 0.0),
    "GFZ Data Services": ("GFZ Data", 0.0), "GFZ Publication Database": ("GFZ Pub", 0.0),
    "ETH Zurich (Data Collection)": ("ETH Data", 0.0), "ETH Zurich (Dataset)": ("ETH Data", 0.0),
    "ETH Zurich (Doctoral Thesis)": ("ETH PhD", 0.0), "ETH Zurich (Master Thesis)": ("ETH MSc", 0.0),
    "Stanford Digital Repository": ("Stanford DR", 0.0), "NSF Seismological Facility for the Advancement of Geoscience (SAGE)": ("SAGE", 0.0),
    "International Snow Science Workshop (ISSW)": ("ISSW", 0.0),
}

# Raw name (lowercase) -> the name Lite files it under.
LITE_ALIASES: Dict[str, str] = {
    "geophysical journal international": "GJI",
    "geophysical research letters": "GRL",
    "earth and planetary science letters": "EPSL",
    "bulletin of the seismological society of america": "BSSA",
    "journal of geophysical research: solid earth": "JGR: Solid Earth",
    "journal of geophysical research: machine learning and computation": "JGR: Machine Learning and Computation",
    "journal of geophysical research earth surface": "JGR: Earth Surface",
    "journal of geophysical research planets": "JGR: Planets",
    "arxiv (cornell university)": "arXiv",
    "international journal of greenhouse gas control": "International Journal of Greenhouse Gas Control",
    "earth system science data": "Earth System Science Data",
    "natural hazards and earth system sciences": "Natural Hazards and Earth System Sciences",
    "international journal of physical modelling in geotechnics": "International Journal of Physical Modelling in Geotechnics",
    "zenodo (cern european organization for nuclear research)": "Zenodo",
    "hal (le centre pour la communication scientifique directe)": "HAL",
    "publication database gfz (gfz german research centre for geosciences)": "GFZ Publication Database",
    "osti oai (u.s. department of energy office of scientific and technical information)": "OSTI",
}

# Preprint servers, repositories, theses, datasets and meetings, matched on the raw journal name.
# re.ASCII gives the same word boundaries as the JavaScript original.
MISC_VENUE = re.compile(
    r"\b(arxiv|ess open archive|eartharxiv|research square|zenodo|figshare|hal|osti|workflowhub|repository"
    r"|data services|data collection|dataset|thesis|general assembly|discussions|computing center"
    r"|publication database|seismological facility|copernicus|workshop)\b",
    re.IGNORECASE | re.ASCII,
)

GROUP_IF = 0         # impact factor listed
GROUP_OTHER = 1      # other journals
GROUP_PREPRINT = 2   # preprints and open platforms

# The journal key of the "Others" placeholder; papers without a journal share it.
OTHERS_KEY = "others"

_DASH_RE = re.compile(r"[–—]+")  # en / em dash: Lite's normaliser drops them, the app's canonicaliser keeps them
_AMP_RE = re.compile(r"\s*&\s*")
_WS_RE = re.compile(r"\s+")


def normalize_journal(name: Optional[str]) -> str:
    """Lookup form of a journal name: the app's shared canonicaliser (lowercase, accents and punctuation
    stripped), then dashes dropped, "&" read as "and" and a leading "the" removed."""
    s = _DASH_RE.sub(" ", canonicalize(name or ""))
    s = _WS_RE.sub(" ", _AMP_RE.sub(" and ", s)).strip()
    return s[4:] if s.startswith("the ") else s


def _build_table() -> Dict[str, Tuple[str, float, str]]:
    """Normalised name -> (short name, impact factor, journal key).

    The names of one journal (same short name and impact factor) share one journal key, their longest
    normalised name, so a sort keeps their papers in one block.
    """
    spellings: Dict[Tuple[str, float], list] = {}
    for name, (short, jif) in JOURNALS.items():
        spellings.setdefault((short, jif), []).append(normalize_journal(name))
    table: Dict[str, Tuple[str, float, str]] = {}
    for (short, jif), names in spellings.items():
        key = min(names, key=lambda n: (-len(n), n))
        for n in names:
            table[n] = (short, jif, key)
    for raw, target in LITE_ALIASES.items():
        entry = table.get(normalize_journal(target))
        if entry is not None:
            table.setdefault(normalize_journal(raw), entry)
    return table


_TABLE: Dict[str, Tuple[str, float, str]] = _build_table()


def lookup(journal: Optional[str]) -> Optional[Tuple[str, float]]:
    """(short name, impact factor) of a listed journal, or None."""
    entry = _TABLE.get(normalize_journal(journal))
    return (entry[0], entry[1]) if entry else None


@lru_cache(maxsize=4096)
def impact_factor_rank(journal: Optional[str]) -> Tuple[int, float, str]:
    """(group, -impact factor, journal key) of a journal name; ascending order is the "Journal IF" order."""
    raw = (journal or "").strip()
    norm = normalize_journal(raw)
    entry = _TABLE.get(norm)
    if MISC_VENUE.search(raw):
        group = GROUP_PREPRINT
    elif entry is not None and entry[1] > 0:
        group = GROUP_IF
    else:
        group = GROUP_OTHER
    if norm in ("", OTHERS_KEY):
        return (group, 1.0, OTHERS_KEY)  # impact factor -1: last among the other journals, as in Lite
    jif = entry[1] if entry is not None else 0.0
    return (group, -jif, entry[2] if entry is not None else norm)


_YEAR_RE = re.compile(r"\d{4}")
_YEAR_MONTH_RE = re.compile(r"\d{4}-\d{2}")


def newest_first(paper: Dict[str, Any]) -> str:
    """Date part of the key, newest first, as Lite orders dates: a year-only date goes after the full dates of
    that year (and a year-month after the days of that month), and a paper without a date goes last."""
    date = str(paper.get("published_date") or "").strip()
    if not date:
        return "~"  # above every inverted date
    if _YEAR_RE.fullmatch(date):
        date += "-00-00"
    elif _YEAR_MONTH_RE.fullmatch(date):
        date += "-00"
    return _invert_iso(date)


def impact_factor_sort_key(paper: Dict[str, Any]) -> Tuple[int, float, str, str, Any]:
    """Sort key for ``sort_by=impact_factor``: (group, -impact factor, journal key, newest first, id)."""
    return impact_factor_rank(paper.get("journal")) + (newest_first(paper), paper.get("id") or "")


__all__ = [
    "JOURNALS",
    "LITE_ALIASES",
    "MISC_VENUE",
    "GROUP_IF",
    "GROUP_OTHER",
    "GROUP_PREPRINT",
    "OTHERS_KEY",
    "normalize_journal",
    "lookup",
    "impact_factor_rank",
    "newest_first",
    "impact_factor_sort_key",
]
