import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import feedparser
import requests
from sqlalchemy import func

logger = logging.getLogger(__name__)

from models import Paper
from services.journal_registry import (
    BROAD,
    PURE,
    CULLED_JOURNAL_NAMES,
    journals_by_issn,
    rss_sources,
    tier_for,
)


# Core matching logic ported directly from 1_fetch_papers.py.
DAYS_LOOKBACK = 30
MIN_ABSTRACT_CHARS = 200
HTTP_TIMEOUT = 25
SLEEP_BETWEEN_CALLS_SEC = 0.1
DISABLE_SSL_VERIFY = False
ENABLE_RSS = True
ENABLE_OPENALEX = True
ENABLE_CROSSREF = True
ENABLE_ETH_RC = True

ETH_RC_BASE_URL = "https://api.library.ethz.ch/research-collection/v2"
ETH_RC_SEARCH_TERMS = [
    "full-waveform inversion",
    "adjoint tomography",
    "waveform tomography",
    "distributed acoustic sensing",
    "DAS seismology",
    "fibre-optic seismology",
    "cryoseismology",
    "icequake",
    "glacial earthquake",
    "ultrasound tomography",
    "medical ultrasound imaging",
    "seismic wave propagation",
    "seismic wavefield simulation",
    "ambient noise tomography",
    "seismic interferometry",
    "noise cross-correlation",
    "Bayesian inversion seismic",
    "Hamiltonian Monte Carlo inversion",
]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SeismoPaperBot/3.1; +https://example.org/bot)"
}

# BRIEF v2 §4 — the journal universe now lives in ONE place
# (services/journal_registry). ``RSS_SOURCES`` and ``JOURNALS_BY_ISSN`` are no
# longer hand-maintained literals here; they are DERIVED from the registry so
# adding a journal is a one-line edit there and the intake lists can never drift
# from the sidebar abbrev map. ``rss_sources()`` / ``journals_by_issn()`` return
# the exact ``{"journal", "rss"}`` / ``{"journal", "issn"}`` shapes this module's
# fetchers already iterate (using the historical fetch/rss labels so the
# ``paper.journal`` column stays byte-identical to the pre-registry pipeline).
#
# We materialise them once at import as module-level lists (not function calls in
# the hot loop) so the read surface is unchanged for any external reader and the
# per-source fetchers below keep iterating ``RSS_SOURCES`` / ``JOURNALS_BY_ISSN``
# verbatim.
RSS_SOURCES: List[Dict[str, str]] = rss_sources()

JOURNALS_BY_ISSN: List[Dict[str, Any]] = journals_by_issn()

JOURNAL_ALIASES_TO_ISSN_CANON = {
    "GJI (Advance Access)": "GJI",
    "GJI (Current Issue)": "GJI",
}

INSTITUTION_PUBLISHER_BLACKLIST = {
    "american association for the advancement of science",
    "american geophysical union",
    "cambridge university press",
    "copernicus publications",
    "elsevier",
    "geoscienceworld",
    "oxford university press",
    "springer",
    "springer nature",
    "wiley",
    "wiley blackwell",
}

INSTITUTION_ACRONYM_REPLACEMENTS = {
    "Cnrs": "CNRS",
    "Csem": "CSEM",
    "Csic": "CSIC",
    "Dtu": "DTU",
    "Epfl": "EPFL",
    "Eth": "ETH",
    "Ethz": "ETHZ",
    "Gfz": "GFZ",
    "Ingv": "INGV",
    "Ipgp": "IPGP",
    "Jpl": "JPL",
    "Kaust": "KAUST",
    "Lmu": "LMU",
    "Mit": "MIT",
    "Nasa": "NASA",
    "Noaa": "NOAA",
    "Ucla": "UCLA",
    "Ucsd": "UCSD",
    "Usgs": "USGS",
}

KEYWORDS: Dict[str, List[str]] = {
    "Full-waveform inversion (FWI) & Adjoint Imaging": [
        r"\bfull[- ]waveform inversion\b",
        r"\bFWI\b",
        r"\belastic full[- ]waveform inversion\b",
        r"\badjoint tomography\b",
        r"\badjoint[- ]state\b",
        r"\bwaveform tomography\b",
        r"\bseismic waveform inversion\b",
        r"\bwaveform misfit\b",
        r"\bfinite[- ]frequency seismic tomography\b",
        r"\btime[- ]lapse full[- ]waveform inversion\b",
        r"\bsource[- ]structure trade[- ]off\b",
    ],
    "Fibre-optic seismology / DAS": [
        r"\bdistributed acoustic sensing\b",
        r"\bDAS\b",
        r"\bdistributed strain sensing\b",
        r"\bDSS\b",
        r"\bphase[- ]?OTDR\b",
        r"\bphi[- ]?OTDR\b",
        r"\bcoherent OTDR\b",
        r"\bfibre[- ]optic seismolog(?:y|ical)\b",
        r"\bfiber[- ]optic seismolog(?:y|ical)\b",
        r"\bsubmarine cable seismolog(?:y|ical)\b",
        r"\btelecom(?:munication)? cable seismolog(?:y|ical)\b",
        r"\btransoceanic cable seismolog(?:y|ical)\b",
        r"\boptical fiber interferometr(?:y|ic)\b",
        r"\bfibre interferometr(?:y|ic)\b",
        r"\bstrain(?:-| )rate (?:measured|measurement) (?:by|with) DAS\b",
        r"\btelecommunication cable as a seismic sensor\b",
    ],
    "Cryosphere / Cryoseismology": [
        r"\bcryoseismolog(?:y|ical)\b",
        r"\bglacier seismolog(?:y|ical)\b",
        r"\bicequake(?:s)?\b",
        r"\bglacial earthquake(?:s)?\b",
        r"\bice[- ]shelf quake(?:s)?\b",
        r"\bice tremor\b",
        r"\bglacier sliding (?:seismic|seismolog)\b",
        r"\bseismic (?:monitoring|imaging) of (?:glacier|ice)\b",
        r"\bDAS (?:on|along) (?:a )?(?:glacier|ice)\b",
    ],
    "Medical ultrasound imaging": [
        r"\bultrasound tomography\b",
        r"\bmedical ultrasound\b",
        r"\bfull[- ]waveform ultrasound inversion\b",
        r"\bultrasonic full[- ]waveform inversion\b",
        r"\btranscranial ultrasound\b",
        r"\bultrasound elastograph(?:y|ic)\b",
        r"\bacousto[- ]elastic\b",
        r"\bbreast (?:cancer|lesion) (?:detection|imaging)\b",
        r"\bsoft[- ]tissue (?:imaging|characteri[sz]ation)\b",
    ],
    "Numerical wave propagation (seismic) & Large-scale simulation": [
        r"\bseismic wave propagation\b",
        r"\b3[- ]D seismic wave propagation\b",
        r"\b3[- ]D elastic wave propagation\b",
        r"\bseismic wavefield simulation\b",
        r"\banelastic wave propagation\b",
        r"\bviscoelastic wave propagation\b",
        r"\bseismic wave equation solver\b",
        r"\bglobal seismic wavefield\b",
        r"\bregional seismic wavefield\b",
        r"\bhigh[- ]order (?:seismic )?wave propagation\b",
    ],
    "Ambient noise seismology & Interferometry": [
        r"\bambient noise seismolog(?:y|ical)\b",
        r"\bambient noise tomography\b",
        r"\bnoise cross[- ]correlation\b",
        r"\bseismic interferometr(?:y|ic)\b",
        r"\bGreen'?s function retrieval\b",
        r"\bmicroseism(?:s|ic)\b",
        r"\bnoise source inversion\b",
        r"\bambient noise source (?:distribution|model)\b",
    ],
    "Inverse theory & Uncertainty quantification": [
        r"\bHamiltonian Monte Carlo\b",
        r"\bHMC\b",
        r"\bMarkov chain Monte Carlo\b",
        r"\bMCMC\b",
        r"\bBayesian (?:elastic )?(?:full[- ]waveform inversion|tomography|inversion)\b",
        r"\bposterior uncertainty\b",
        r"\bnullspace exploration\b",
        r"\bHamiltonian nullspace shuttle(?:s)?\b",
        r"\brandom probing (?:for )?uncertainty\b",
        r"\btrade[- ]off analysis\b",
        r"\bjoint inversion\b",
        r"\bBackus[-–]Gilbert\b",
    ],
}

EXCLUDE_PATTERNS = [r"\bseizure\b", r"\bcardiac\b", r"\btumou?r\b", r"\bcancer\b"]

_COMPILED_KEYWORDS = {cat: [re.compile(p, re.IGNORECASE) for p in pats] for cat, pats in KEYWORDS.items()}
_COMPILED_EXCLUDE = [re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATTERNS]

# Journal mastheads / non-article front-matter that OpenAlex & Crossref index as
# "papers" (no abstract, no content). Rejected at the gate for ALL tiers — a
# dedicated seismology journal's editorial board is still not a paper.
_MASTHEAD_RE = re.compile(
    r"^\s*(editorial board|issue information|(table of )?contents|masthead|"
    r"(front|back)?\s*cover(\s*image)?|reviewer acknowledg\w*|author index|"
    r"subject index|in this issue|list of (reviewers|contributors))\b",
    re.IGNORECASE,
)


def _is_masthead(text: str) -> bool:
    return bool(_MASTHEAD_RE.match((text or "").strip()))
_DOI_REGEX = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Domain intake gate (BRIEF v2 §5)
# ---------------------------------------------------------------------------
#
# The narrow 7-topic ``KEYWORDS`` regex above used to be the ONLY intake gate AND
# the per-topic selector. BRIEF v2 retires it as the per-topic selector (that
# now lives in the predicate-based trackers). Intake is now a single uniform
# gate keyed off the domain filter below — there is NO per-tier bypass:
#
#   * EVERY venue (whether the registry labels it ``tier="pure"`` or
#     ``tier="broad"``) is admitted only if it clears the domain filter. A
#     dedicated seismology journal carries plenty of off-topic earth science,
#     so it is gated just like a multidisciplinary venue (Nature/Science/PNAS/
#     Sci Adv/Nat Comms and the GRL/JGR umbrella, plus cross-disciplinary titles
#     like IEEE TGRS). ``tier`` is display/tracking metadata only and grants no
#     intake bypass.
#   * The ONLY bypass is ``explicit_follow`` (journal-only / author-only /
#     arxiv_category trackers — the user named that source).
#
# The domain filter is intentionally WIDE — it is a "is this Earth-structure /
# seismic / geophysical signal science at all?" net, not the focused 7-topic
# regex. A paper is admitted if it matches the wide net OR any of the original
# focused ``KEYWORDS`` (so anything the narrow gate used to keep is still kept).
# The publisher/medical EXCLUDE_PATTERNS still veto.
BROAD_DOMAIN_KEYWORDS: List[str] = [
    # Core seismology / earthquakes
    r"\bseismic\b",
    r"\bseismolog(?:y|ical|ist)\b",
    r"\bseismicit(?:y|ies)\b",
    r"\bseismogram(?:s)?\b",
    r"\bseismometer(?:s)?\b",
    r"\bearthquake(?:s)?\b",
    r"\baftershock(?:s)?\b",
    r"\bforeshock(?:s)?\b",
    r"\bmainshock(?:s)?\b",
    r"\bmicroseismic(?:ity)?\b",
    r"\bmoment tensor\b",
    r"\bfocal mechanism(?:s)?\b",
    r"\bground motion\b",
    r"\bground[- ]shaking\b",
    r"\bpeak ground acceleration\b",
    r"\bshakemap\b",
    r"\bfault(?:ing|s)?\b",
    r"\brupture(?:s|d)?\b",
    r"\bslow slip\b",
    r"\btremor(?:s)?\b",
    r"\bhypocent(?:er|re|ral)\b",
    r"\bepicent(?:er|re|ral)\b",
    r"\bmagnitude\b",
    # Waves / wavefield / propagation
    r"\bseismic wave(?:s|field)?\b",
    r"\bwave propagation\b",
    r"\bbody wave(?:s)?\b",
    r"\bsurface wave(?:s)?\b",
    r"\bRayleigh wave(?:s)?\b",
    r"\bLove wave(?:s)?\b",
    r"\bP[- ]wave(?:s)?\b",
    r"\bS[- ]wave(?:s)?\b",
    r"\bambient noise\b",
    r"\bcoda wave(?:s)?\b",
    # Imaging / inversion / tomography
    r"\btomograph(?:y|ic)\b",
    r"\bfull[- ]waveform inversion\b",
    r"\bwaveform inversion\b",
    r"\bseismic inversion\b",
    r"\badjoint\b",
    r"\bmigration imaging\b",
    r"\bvelocity model\b",
    r"\bsubsurface imaging\b",
    r"\binterferometr(?:y|ic)\b",
    # Solid Earth / geophysics / interior
    r"\bgeophysic(?:s|al)\b",
    r"\bgeodynamic(?:s)?\b",
    r"\btectonic(?:s)?\b",
    r"\bgeodes(?:y|tic)\b",
    r"\bGNSS\b",
    r"\bInSAR\b",
    r"\bcrust(?:al)?\b",
    r"\bmantle\b",
    r"\blithospher(?:e|ic)\b",
    r"\basthenospher(?:e|ic)\b",
    r"\bsubduction\b",
    r"\bplate boundary\b",
    r"\bvolcan(?:o|ic|ism|oes)\b",
    r"\bmagma(?:tic)?\b",
    r"\bgeothermal\b",
    r"\bEarth'?s interior\b",
    r"\bEarth structure\b",
    # Instrumentation / sensing relevant to seismology
    r"\bdistributed acoustic sensing\b",
    r"\bDAS\b",
    r"\bfib(?:re|er)[- ]optic\b",
    r"\bocean[- ]bottom seismomet\w*\b",
    # Hazard
    r"\bseismic hazard\b",
    r"\bseismic risk\b",
    r"\btsunami(?:s)?\b",
    r"\bliquefaction\b",
    # Cryo / glacier seismology
    r"\bcryoseismolog(?:y|ical)\b",
    r"\bicequake(?:s)?\b",
    r"\bglacial earthquake(?:s)?\b",
    # Methods / ML that are squarely geophysical when paired with the above —
    # kept narrow (require an explicit geophysical noun nearby via the other
    # patterns; these standalone terms intentionally omitted to avoid flooding).
]

_COMPILED_BROAD_DOMAIN = [re.compile(p, re.IGNORECASE) for p in BROAD_DOMAIN_KEYWORDS]


# 2026-06-12 precision rework (see _audit_2026-06-11/05-CLEANUP-EXECUTED.md).
# The old "ANY one BROAD_DOMAIN keyword" gate admitted 61% off-domain junk on a
# real 155-paper corpus: bearing "fault" diagnosis, "order of magnitude", membrane
# "rupture" (medical), terahertz "interferometry", GNSS traffic monitoring — all
# slipped in via a single polysemous keyword. New rule: admit iff the text has
# ANY ONE *unambiguous* strong-core seismology term, OR >=2 distinct *medium*
# (polysemous) terms. Validated on the labeled corpus: precision 38% -> 91%,
# blocks 96% of off-domain junk while keeping 88% of core seismology. (Layer 2,
# an optional LLM domain gate for broad-tier venues, catches the residue.)
_DOMAIN_STRONG_CORE = [
    r"\bearthquakes?\b", r"\bseismolog(?:y|ical|ist)\b", r"\bseismicit(?:y|ies)\b",
    r"\bseismograms?\b", r"\bseismomet(?:er|ers|ry)\b", r"\baftershocks?\b",
    r"\bforeshocks?\b", r"\bmainshocks?\b", r"\bmicroseismic(?:ity)?\b",
    r"\bhypocent(?:er|re|ral)\b", r"\bepicent(?:er|re|ral)\b", r"\bmoment tensor\b",
    r"\bfocal mechanisms?\b", r"\bground motion\b", r"\bground[- ]shaking\b",
    r"\bpeak ground accel\w*\b", r"\bstrong[- ]motion\b", r"\bshakemap\b",
    r"\bseismic hazard\b", r"\bseismic risk\b", r"\bslow slip\b",
    r"\b(?:non[- ]?volcanic |seismic |tectonic )tremor\b", r"\bcoda waves?\b",
    r"\bfull[- ]?waveform inversion\b", r"\breceiver functions?\b",
    r"\bgutenberg[- ]richter\b", r"\bambient[- ]noise\b",
    r"\bocean[- ]bottom seismomet\w*\b", r"\bphase picking\b", r"\bseismic phases?\b",
    r"\bearthquake rupture\b", r"\bcoseismic\b", r"\bcryoseismolog\w*\b",
    r"\bicequakes?\b", r"\bglacial earthquakes?\b", r"\bteleseismic\b",
]
_DOMAIN_MEDIUM = [
    r"\bseismic\b", r"\bfault(?:ing|s)?\b", r"\brupture(?:s|d)?\b",
    r"\btomograph(?:y|ic)\b", r"\bwaveforms?\b", r"\bsubduction\b", r"\bcrust(?:al)?\b",
    r"\bmantle\b", r"\blithospher(?:e|ic)\b", r"\btectonics?\b", r"\bmagma(?:tic)?\b",
    r"\bvolcan(?:o|ic|ism|oes)\b", r"\bshear[- ]waves?\b", r"\bbody waves?\b",
    r"\bsurface waves?\b", r"\bRayleigh waves?\b", r"\bLove waves?\b",
    r"\bvelocity (?:model|structure|anomal\w*)\b", r"\bdistributed acoustic sensing\b",
    r"\bfib(?:re|er)[- ]optic\b", r"\btremors?\b", r"\bInSAR\b", r"\bGNSS\b",
]
_COMPILED_STRONG_CORE = [re.compile(p, re.IGNORECASE) for p in _DOMAIN_STRONG_CORE]
_COMPILED_MEDIUM = [re.compile(p, re.IGNORECASE) for p in _DOMAIN_MEDIUM]


# Map EVERY historical fetch/rss label this module iterates (which may differ
# from the registry's canonical title, e.g. "GJI", "Geophysics (SEG)",
# "JGR: Solid Earth", "GJI (Current Issue)") to its registry tier. Built once
# from the registry's own ``fetch_name`` / ``rss_name`` provenance so the gate
# resolves the same row whether intake came in via OpenAlex/Crossref (fetch
# label) or RSS (rss label). Falls back to ``tier_for(canonical_title)``.
def _build_label_tier_map() -> Dict[str, str]:
    table: Dict[str, str] = {}
    from services.journal_registry import JOURNALS as _REG_JOURNALS

    for entry in _REG_JOURNALS:
        tier = str(entry.get("tier") or "")
        if not tier:
            continue
        for key in (
            entry.get("name"),
            entry.get("fetch_name"),
            entry.get("rss_name"),
        ):
            if key:
                table.setdefault(str(key), tier)
    return table


_LABEL_TIER_MAP: Dict[str, str] = _build_label_tier_map()


def tier_for_label(journal_label: Optional[str]) -> Optional[str]:
    """Resolve a fetch/rss journal label (or canonical title) to its tier.

    Returns ``"pure"`` / ``"broad"`` / ``None``. Tries the historical-label map
    first (covers fetch_service's own labels like "Geophysics (SEG)"), then the
    registry's canonical-title resolver. ``None`` for venues not in the registry
    (e.g. ad-hoc ETH-RC journal strings) — the caller decides the default.
    """
    if not journal_label:
        return None
    label = str(journal_label).strip()
    if not label:
        return None
    hit = _LABEL_TIER_MAP.get(label)
    if hit:
        return hit
    return tier_for(label)


def in_seismic_domain(text: str) -> bool:
    """Broad seismology / geophysics domain filter for ``tier="broad"`` venues.

    Returns ``True`` when ``text`` looks like Earth-structure / seismic /
    geophysical-signal science *at all* — the wide net of
    :data:`BROAD_DOMAIN_KEYWORDS` OR any of the focused :data:`KEYWORDS` (so
    nothing the narrow gate historically admitted is lost). The medical /
    publisher :data:`EXCLUDE_PATTERNS` still veto. An empty/None text -> False.
    """
    if not text:
        return False
    for bad in _COMPILED_EXCLUDE:
        if bad.search(text):
            return False
    # Precision gate (2026-06-12): one unambiguous strong-core seismology term is
    # enough; otherwise require >=2 distinct medium (polysemous) terms so a lone
    # "fault"/"magnitude"/"rupture"/"interferometry" can't admit a bearing-fault,
    # battery, or medical paper. See _DOMAIN_STRONG_CORE / _DOMAIN_MEDIUM above.
    if any(p.search(text) for p in _COMPILED_STRONG_CORE):
        return True
    medium_hits = sum(1 for p in _COMPILED_MEDIUM if p.search(text))
    return medium_hits >= 2


# 2026-06-14: venues the owner culled in the preset review — hard-rejected at
# intake regardless of keywords/tier. The registry already drops them so they're
# never fetched-by-ISSN; this set catches them when they arrive via broad sources
# (eth_rc / OpenAlex-by-author / Crossref-prefix preprints).
_CULLED_JOURNALS_NORM = {n.strip().lower() for n in CULLED_JOURNAL_NAMES}


def passes_domain_gate(journal_label: Optional[str], text: str) -> bool:
    """Domain intake gate — EVERY venue must pass the same filter.

    The contract is a single uniform gate: a culled venue is hard-rejected,
    masthead front-matter is rejected, and then EVERY venue (pure AND broad
    alike) is admitted only if :func:`in_seismic_domain` (the wide
    seismology/geophysics net, minus the medical/publisher EXCLUDE veto) passes.
    ``journal_label`` / ``tier`` are still carried in the registry for display
    and tracking, but grant NO intake bypass here.

    Explicit follows (journal-only / author-only / arxiv_category trackers) do
    NOT call this — they bypass the gate via the ``explicit_follow`` flag (the
    user named that source, so they get all of it). That is the ONLY bypass.
    """
    if journal_label and journal_label.strip().lower() in _CULLED_JOURNALS_NORM:
        return False  # 2026-06-14: owner-culled venue — hard reject regardless of tier/keywords
    if _is_masthead(text):
        return False  # journal front-matter (editorial board / contents / cover) is not a paper
    # 2026-06-14: pure venues NO LONGER bypass the gate. Even dedicated journals
    # (EPSL, Tectonophysics, Geophysics, G3, Geophysical Prospecting…) carry plenty
    # of non-seismology earth science, so EVERY paper must clear the seismology
    # keyword gate here (+ the Layer-2 LLM gate downstream). Only explicit_follow
    # author/arXiv tracker pulls still bypass (the user named that source).
    return in_seismic_domain(text)


# ---------------------------------------------------------------------------
# Layer-2 domain gate (2026-06-12): a cheap LLM relevance filter for BROAD-tier
# venues. in_seismic_domain (Layer 1) blocks ~96% of off-domain junk, but a few
# polysemous-but-plausible papers slip through and the gray-zone (adjacent
# geophysics) is hard to nail with keywords. A Flash yes/no classifier over the
# BROAD-tier survivors fixes both. Pure-tier venues (dedicated seismology
# journals) are trusted and skipped. Degrades gracefully: disabled, no key,
# timeout, or an unparseable reply all KEEP the paper — the gate only ever drops
# on an explicit False, so it can never silently empty a fetch.
#   env SEISMO_LLM_DOMAIN_GATE=0    -> disable entirely
#   env SEISMO_DOMAIN_GATE_MODEL    -> override model (default gemini-3.8-flash)
# 2026-10-16: Gemini 2.5 retires on Vertex 2026-10-20, so the default moves from
# gemini-2.5-flash to gemini-3.8-flash (GA, retires >= 2027-09-02). A/B through
# _llm_domain_classify on 25 kept DB papers + 25 domain-refilter deletions: 3.8-flash
# matched 47/50 (2.5-flash 46/50, Flash-Lite models 43-44/50) and kept none of the
# off-domain items. Earlier note: see _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
# ---------------------------------------------------------------------------
_DOMAIN_GATE_MODEL = (os.environ.get("SEISMO_DOMAIN_GATE_MODEL") or "gemini-3.8-flash").strip()
_DOMAIN_GATE_CHUNK = 25


def _llm_domain_classify(items: List[Tuple[str, Optional[str], Optional[str], Optional[str]]]) -> Dict[str, bool]:
    """Classify a chunk of (key, journal, title, abstract). Return {key: bool}
    (True = keep as seismology). Returns {} on ANY failure so the caller keeps all."""
    try:
        from services.embedding_service import get_embedding_client
        client = get_embedding_client()
        if client is None:
            return {}
    except Exception:
        return {}
    lines = [
        f'{k}\t[{journal or ""}] {(title or "").strip()} :: {(abstract or "")[:300]}'
        for (k, journal, title, abstract) in items
    ]
    prompt = (
        "You are a STRICT relevance classifier for a solid-Earth SEISMOLOGY "
        "literature tool. For each paper, decide if its PRIMARY subject is "
        "seismology / earthquake science / seismic-wave methods / seismic "
        "instrumentation — INCLUDING machine learning applied to seismic or "
        "earthquake data, and solid-Earth geophysics (tomography, tectonics, "
        "volcano seismology, geodynamics). It is NOT relevant if its core topic "
        "is generic ML, mechanical/bearing fault diagnosis, materials, batteries, "
        "medical/biology, traffic, agriculture/vegetation remote sensing, telecom "
        "engineering, oceanography, atmosphere, or space weather — even when it "
        "mentions words like 'fault', 'magnitude', 'rupture', 'seismic' or 'GNSS'.\n"
        "Return ONLY a JSON object mapping each paper's leading key token to true "
        "(seismology) or false. No prose, no code fence.\n\nPapers:\n" + "\n".join(lines)
    )
    try:
        resp = client.models.generate_content(model=_DOMAIN_GATE_MODEL, contents=prompt)
        txt = (getattr(resp, "text", "") or "").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            return {}
        raw = json.loads(m.group(0))
        out: Dict[str, bool] = {}
        for k, v in raw.items():
            out[str(k)] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
        return out
    except Exception:
        return {}


def llm_domain_keep_ids(
    papers_data: List[Dict[str, Any]],
    *,
    progress_callback: Optional[Callable[..., None]] = None,
) -> set:
    """Return the set of paper ids to KEEP after the Layer-2 LLM domain gate.

    2026-06-14: ALL venues are classified (pure included) — dedicated journals
    carry off-topic earth science too, so they get the same Flash yes/no check.
    Only ``explicit_follow`` papers (author/arXiv tracker pulls — the user named
    that source) skip. Any paper the classifier omits or that errors out is KEPT;
    a paper is dropped ONLY on an explicit ``False`` verdict.
    """
    all_ids = {p["id"] for p in papers_data}
    if os.environ.get("SEISMO_LLM_DOMAIN_GATE", "1") != "1":
        return all_ids
    # explicit_follow papers (author/arXiv tracker-driven intake — the user named
    # that source) bypass the domain gate entirely: a watchlist author's paper is
    # kept even if its topic drifts off-seismology. EVERY other paper (pure or
    # broad journal alike) is LLM-classified.
    broad = [
        p for p in papers_data
        if not p.get("explicit_follow")
    ]
    if not broad:
        return all_ids
    drop: set = set()
    for start in range(0, len(broad), _DOMAIN_GATE_CHUNK):
        chunk = broad[start:start + _DOMAIN_GATE_CHUNK]
        verdict = _llm_domain_classify(
            [(p["id"], p.get("journal"), p.get("title"), p.get("abstract")) for p in chunk]
        )
        for p in chunk:
            if verdict.get(p["id"]) is False:
                drop.add(p["id"])
    if drop:
        _emit(progress_callback, f"LLM domain gate dropped {len(drop)} off-domain paper(s)", stage="fetch_store")
    return all_ids - drop


def _emit(progress_callback: Optional[Callable[..., None]], message: str, **kwargs: Any) -> None:
    if progress_callback:
        progress_callback(message, **kwargs)
    else:
        print(message, flush=True)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_entry_datetime(entry) -> Optional[dt.datetime]:
    for key in ("published", "updated", "created"):
        value = getattr(entry, key, None)
        if value:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
            except Exception:
                pass
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            try:
                return dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
            except Exception:
                pass
    return None


def match_keywords(text: str) -> Optional[List[str]]:
    if not text:
        return None
    for bad in _COMPILED_EXCLUDE:
        if bad.search(text):
            return None
    hits = []
    for category, patterns in _COMPILED_KEYWORDS.items():
        if any(pattern.search(text) for pattern in patterns):
            hits.append(category)
    return hits or None


def stable_id(*parts: str) -> str:
    raw = "||".join([part or "" for part in parts]).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def dedup_key(*, doi: str = "", link: str = "", title: str = "") -> str:
    """Source-INDEPENDENT identity for a paper (P1-1, 2026-06-11).

    The same DOI/title/link arriving from any source (rss/openalex/crossref/
    eth_rc/arxiv) maps to ONE id, so the shared per-run ``seen`` set dedups
    across sources and we never store the same paper twice. Prefers normalized
    DOI, then canonicalized title, then link. The leading segment is a semantic
    prefix ("doi"/"title"/"link") — NOT the source name — which is what makes the
    key source-independent. See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
    """
    nd = normalize_doi(doi)
    if nd:
        # DOIs are case-INSENSITIVE (DOI Handbook): OpenAlex returns lowercase,
        # Crossref/RSS often keep registration casing (e.g. 10.1029/2024JB...).
        # Lowercase the key so the same DOI from any source dedups to one row.
        # (We lowercase only the dedup KEY, not the stored paper.doi.)
        return stable_id("doi", nd.lower())
    nt = _canonical_text_key(title)
    if nt:
        return stable_id("title", nt)
    # No DOI and no canonical title: fall back to raw title+link so distinct
    # malformed entries don't all collapse to one constant id. (Entries with an
    # empty title AND empty link are genuinely identity-less and still collapse.)
    return stable_id("raw", (title or "").strip(), link or "")


def _is_empty(value: Any) -> bool:
    """True for None / blank string / empty list|dict|tuple|set (P1-2 fill-empty)."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


# P1-2 source-of-truth ranking: higher = more authoritative. A weaker source
# (periodic RSS re-fetch) must not clobber fields a stronger source already
# wrote. Unknown sources rank 0 (treated as weakest). See ACTION-PLAN P1-2.
SOURCE_RANK = {"rss": 1, "eth_rc": 2, "arxiv": 2, "openalex": 3, "crossref": 3}


def _source_rank(source: Optional[str]) -> int:
    return SOURCE_RANK.get((source or "").strip().lower(), 0)


def validate_feed(url: str) -> Tuple[bool, str]:
    try:
        feed = feedparser.parse(url, request_headers=REQUEST_HEADERS)
        if getattr(feed, "bozo", 0):
            return False, f"bozo={type(getattr(feed, 'bozo_exception', None)).__name__}"
        if not getattr(feed, "entries", []):
            return False, "no entries"
        return True, "ok"
    except Exception as exc:
        return False, f"exception={exc}"


def http_get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=HTTP_TIMEOUT,
        verify=(not DISABLE_SSL_VERIFY),
    )
    response.raise_for_status()
    return response.json()


def openalex_abstract_from_inverted_index(inv: Optional[Dict[str, List[int]]]) -> str:
    if not inv:
        return ""
    positions = []
    for word, pos_list in inv.items():
        for pos in pos_list:
            positions.append((pos, word))
    positions.sort(key=lambda item: item[0])
    return " ".join([word for _, word in positions])


def normalize_doi(doi: Optional[str]) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip()
    return doi


def extract_doi_from_rss_entry(entry) -> str:
    candidates = []
    for key in ("dc_identifier", "prism_doi", "doi"):
        if key in entry:
            candidates.append(str(entry.get(key) or ""))

    link = str(entry.get("link") or "")
    entry_id = str(entry.get("id") or "")
    summary = str(entry.get("summary") or entry.get("description") or "")
    candidates += [link, entry_id, summary]

    for candidate in candidates:
        match = _DOI_REGEX.search(candidate)
        if match:
            return normalize_doi(match.group(1))
    return ""


def get_abstract_flag(abstract: str) -> str:
    abstract = (abstract or "").strip()
    if not abstract:
        return "missing"
    return "ok" if len(abstract) >= MIN_ABSTRACT_CHARS else "short"


def normalize_topics(categories: Optional[List[str]]) -> List[str]:
    clean_categories = [category.strip() for category in (categories or []) if category and category.strip()]
    # 2026-06-13: canonicalize legacy / renamed labels at ingest so newly-fetched
    # papers can't reintroduce dropped vocabulary (mirrors the init_db rename
    # migration; analysis already coerces, but un-analyzed RSS/BibTeX categories
    # would otherwise pass through raw).
    from services.cos_topics import COS_TOPIC_RENAMES, PRIMARY_TOPIC_RENAMES, PRIMARY_TOPIC_DROPS
    _renames = {**COS_TOPIC_RENAMES, **PRIMARY_TOPIC_RENAMES}
    _drops = set(PRIMARY_TOPIC_DROPS)
    out: List[str] = []
    for c in clean_categories:
        if c in _drops:
            continue
        c = _renames.get(c, c)
        if c not in out:
            out.append(c)
    return out or ["Others"]


def _canonical_text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _is_publisher_like_institution(value: str) -> bool:
    return _canonical_text_key(value) in INSTITUTION_PUBLISHER_BLACKLIST


def _normalize_institution_display_name(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        return ""

    # Some sources (Crossref / ETH RC raw affiliations) glue the author's
    # affiliation-index marker onto the front of the string, e.g.
    # "1Department of Geophysics, Stanford University …", "2 Institute …" or
    # "3now at, MIT Lincoln Laboratory …". Strip a leading 1–2 digit index
    # (with optional . ) ] separator) when a word follows, but NEVER when the
    # digits are an ordinal ("21st", "2nd", "10th", …), which are real words.
    text = re.sub(
        r"^\d{1,2}(?!(?:st|nd|rd|th)\b)[.)\]]?\s*(?=[A-Za-zÀ-Þ])", "", text
    ).strip()
    if not text:
        return ""

    has_letters = bool(re.search(r"[A-Za-z]", text))
    if has_letters and (text == text.upper() or text == text.lower()):
        text = text.title()
        for source, target in INSTITUTION_ACRONYM_REPLACEMENTS.items():
            text = re.sub(rf"\b{re.escape(source)}\b", target, text)
        text = text.replace("Mcgill", "McGill")

    return text


def normalize_institutions(institutions: Optional[List[str]]) -> List[str]:
    clean_institutions = []
    seen = set()
    for institution in institutions or []:
        value = _normalize_institution_display_name(institution)
        if not value or _is_publisher_like_institution(value):
            continue
        canonical = _canonical_text_key(value)
        if canonical and canonical not in seen:
            clean_institutions.append(value)
            seen.add(canonical)
    return clean_institutions


def normalize_journal(journal: Optional[str]) -> str:
    journal = (journal or "").strip()
    journal = JOURNAL_ALIASES_TO_ISSN_CANON.get(journal, journal)
    return journal or "Others"


def load_eth_rc_api_key() -> Optional[str]:
    key = (os.getenv("ETH_RC_API_KEY") or "").strip()
    return key or None


def _paper_payload(
    *,
    paper_id: str,
    source: str,
    journal: str,
    title: str,
    date: str,
    datetime_utc: str,
    link: str,
    doi: str,
    categories: Optional[List[str]],
    abstract: str,
    abstract_flag: str,
    authors: List[str],
    institutions: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    explicit_follow: bool = False,
    arxiv_category: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "id": paper_id,
        "source": source,
        "journal": normalize_journal(journal),
        "title": title,
        "date": date,
        "datetime_utc": datetime_utc,
        "link": link,
        "doi": normalize_doi(doi),
        "categories": normalize_topics(categories),
        "institutions": normalize_institutions(institutions),
        "keywords": list(keywords or []),
        "abstract": abstract,
        "abstract_flag": abstract_flag or "missing",
        "authors": authors or [],
        # BRIEF v2 §2 — when True this paper came in via an EXPLICIT follow
        # (a journal-only / author-only / arxiv_category tracker, or the targeted
        # fetch_from_arxiv / fetch_from_openalex_by_author paths). Ingest MUST
        # bypass the seismology domain gate for these (the user asked for the
        # source by name). Only the broad default pool is domain-gated.
        "explicit_follow": bool(explicit_follow),
    }
    if arxiv_category:
        payload["arxiv_category"] = str(arxiv_category)
    return payload


def extract_openalex_institutions(work: Dict[str, Any]) -> List[str]:
    institutions = []
    for authorship in work.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            name = (institution.get("display_name") or "").strip()
            if name:
                institutions.append(name)
    return normalize_institutions(institutions)


def extract_openalex_keywords(work: Dict[str, Any], limit: int = 6) -> List[str]:
    """Cheap fetch-time keywords from an OpenAlex work — NO Gemini call.

    Prefers OpenAlex's explicit ``keywords`` field; falls back to ``concepts``
    (ranked by score, skipping overly-broad level-0 fields like "Geology").
    Deduped, capped at ``limit``. Gives ~every OpenAlex paper keywords for free.
    """
    out: List[str] = []
    for kw in work.get("keywords") or []:
        name = (kw.get("display_name") or kw.get("keyword") or "").strip()
        if name:
            out.append(name)
    if not out:
        concepts = sorted(
            (work.get("concepts") or []),
            key=lambda c: c.get("score") or 0.0,
            reverse=True,
        )
        for c in concepts:
            if (c.get("level") or 0) >= 1:  # skip level-0 mega-fields
                name = (c.get("display_name") or "").strip()
                if name:
                    out.append(name)
    seen: set = set()
    result: List[str] = []
    for k in out:
        kl = k.lower()
        if kl and kl not in seen:
            seen.add(kl)
            result.append(k)
        if len(result) >= limit:
            break
    return result


def extract_crossref_institutions(item: Dict[str, Any]) -> List[str]:
    institutions = []
    for author in item.get("author") or []:
        for affiliation in author.get("affiliation") or []:
            name = (affiliation.get("name") or "").strip()
            if name:
                institutions.append(name)
    return normalize_institutions(institutions)


def extract_eth_rc_institutions(metadata: Dict[str, Any]) -> List[str]:
    institutions = []
    candidate_fields = [
        "oairecerif.author.affiliation",
        "dc.contributor.affiliation",
        "dc.contributor.other",
        "ethz.institute",
        "ethz.ethz-institute",
        "ethz.organization",
        "ethz.department",
    ]

    for field in candidate_fields:
        for item in metadata.get(field, []) or []:
            value = (item.get("value", "") or "").strip()
            if value:
                institutions.append(value)

    return normalize_institutions(institutions)


def fetch_from_rss(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    if not ENABLE_RSS:
        return papers

    valid_sources = []
    for source in RSS_SOURCES:
        ok, reason = validate_feed(source["rss"])
        if ok:
            valid_sources.append(source)
        else:
            _emit(progress_callback, f"Skipped RSS source {source['journal']} because {reason}", stage="fetch_rss")

    _emit(progress_callback, f"RSS sources available: {len(valid_sources)}/{len(RSS_SOURCES)}", stage="fetch_rss")

    for source in valid_sources:
        journal = source["journal"]
        source_kept = 0
        _emit(progress_callback, f"Scanning RSS source: {journal}", stage="fetch_rss")
        feed = feedparser.parse(source["rss"], request_headers=REQUEST_HEADERS)
        for entry in getattr(feed, "entries", []) or []:
            pub_dt = parse_entry_datetime(entry)
            if not pub_dt:
                continue

            pub_date = pub_dt.date()
            if pub_date < date_from or pub_date > date_to:
                continue

            title = clean_text(getattr(entry, "title", ""))
            abstract = clean_text(entry.get("summary", "") or entry.get("description", ""))
            link = getattr(entry, "link", "") or ""
            doi = extract_doi_from_rss_entry(entry)

            flag = get_abstract_flag(abstract)
            content = f"{title} {abstract}" if flag == "ok" else title
            # BRIEF v2 §5 domain gate (all venues pass in_seismic_domain).
            # ``matched`` is kept only as best-effort topic LABELS (categories),
            # no longer the gate.
            if not passes_domain_gate(journal, content):
                continue
            matched = match_keywords(content)

            paper_id = dedup_key(doi=doi, link=link, title=title)
            if paper_id in seen:
                continue
            seen.add(paper_id)
            source_kept += 1

            papers.append(
                _paper_payload(
                    paper_id=paper_id,
                    source="rss",
                    journal=journal,
                    title=title,
                    date=pub_dt.date().isoformat(),
                    datetime_utc=pub_dt.isoformat(),
                    link=link,
                    doi=doi,
                    categories=matched,
                    abstract=abstract,
                    abstract_flag=flag,
                    authors=[],
                    institutions=[],
                )
            )
        _emit(progress_callback, f"Finished RSS source {journal}; kept {source_kept} candidate papers", stage="fetch_rss", fetch_source="rss", fetch_source_name=journal)
        time.sleep(SLEEP_BETWEEN_CALLS_SEC)
    return papers


def fetch_from_openalex(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    if not ENABLE_OPENALEX:
        return papers

    base_url = "https://api.openalex.org/works"
    from_date = date_from.isoformat()
    until_date = date_to.isoformat()

    for journal_item in JOURNALS_BY_ISSN:
        issns = journal_item.get("issn") or []
        if not issns:
            continue

        cursor = "*"
        fetched = 0
        kept = 0
        _emit(progress_callback, f"Scanning OpenAlex journal: {journal_item['journal']}", stage="fetch_openalex")
        filter_string = f"from_publication_date:{from_date},to_publication_date:{until_date},primary_location.source.issn:{'|'.join(issns)}"
        while True:
            params = {
                "filter": filter_string,
                "per-page": 200,
                "cursor": cursor,
                "mailto": "example@example.org",
            }
            try:
                data = http_get_json(base_url, params)
            except Exception:
                break

            results = data.get("results", []) or []
            if not results:
                break

            for work in results:
                fetched += 1
                title = clean_text(work.get("title", "") or "")
                doi = normalize_doi(work.get("doi", "") or "")

                link = ""
                primary_location = work.get("primary_location") or {}
                if isinstance(primary_location, dict):
                    link = primary_location.get("landing_page_url") or primary_location.get("pdf_url") or ""
                if not link and doi:
                    link = f"https://doi.org/{doi}"

                pub_date = (work.get("publication_date") or "").strip()
                if not pub_date:
                    continue
                if pub_date < from_date or pub_date > until_date:
                    continue

                abstract = clean_text(openalex_abstract_from_inverted_index(work.get("abstract_inverted_index")))
                flag = get_abstract_flag(abstract)
                content = f"{title} {abstract}" if flag == "ok" else title
                # BRIEF v2 §5 domain gate (see fetch_from_rss).
                if not passes_domain_gate(journal_item["journal"], content):
                    continue
                matched = match_keywords(content)

                authors = []
                for authorship in (work.get("authorships") or []):
                    author_name = (authorship.get("author") or {}).get("display_name")
                    if author_name:
                        authors.append(author_name)

                paper_id = dedup_key(doi=doi, link=link, title=title)
                if paper_id in seen:
                    continue
                seen.add(paper_id)
                kept += 1

                papers.append(
                    _paper_payload(
                        paper_id=paper_id,
                        source="openalex",
                        journal=journal_item["journal"],
                        title=title,
                        date=pub_date,
                        datetime_utc=pub_date,
                        link=link,
                        doi=doi,
                        categories=matched,
                        institutions=extract_openalex_institutions(work),
                        keywords=extract_openalex_keywords(work),
                        abstract=abstract,
                        abstract_flag=flag,
                        authors=authors,
                    )
                )

            next_cursor = (data.get("meta") or {}).get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)
            if fetched >= 1200:
                break
        _emit(progress_callback, f"Finished OpenAlex {journal_item['journal']}; fetched {fetched}, kept {kept}", stage="fetch_openalex")
    return papers


def fetch_from_crossref(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    if not ENABLE_CROSSREF:
        return papers

    base_url = "https://api.crossref.org/works"
    from_date = date_from.isoformat()
    until_date = date_to.isoformat()

    for journal_item in JOURNALS_BY_ISSN:
        issns = journal_item.get("issn") or []
        if not issns:
            continue

        fetched = 0
        kept = 0
        _emit(progress_callback, f"Scanning Crossref journal: {journal_item['journal']}", stage="fetch_crossref")
        for issn in issns:
            cursor = "*"
            while True:
                params = {
                    "filter": f"from-pub-date:{from_date},until-pub-date:{until_date},issn:{issn}",
                    "rows": 200,
                    "cursor": cursor,
                    # 2026-06-14: removed `cursor-max` — Crossref 400s on it, which
                    # had silently killed ALL crossref journal intake (DB had 0
                    # crossref papers). Without it this path works again.
                    "mailto": "example@example.org",
                }
                try:
                    data = http_get_json(base_url, params)
                except Exception:
                    break

                message = data.get("message") or {}
                items = message.get("items") or []
                if not items:
                    break

                for item in items:
                    fetched += 1
                    title_list = item.get("title") or []
                    title = clean_text(title_list[0] if title_list else "")
                    doi = normalize_doi(item.get("DOI") or "")
                    link = f"https://doi.org/{doi}" if doi else ""

                    pub = item.get("published-print") or item.get("published-online") or item.get("created") or {}
                    date_parts = (pub.get("date-parts") or [[None]])[0]
                    if not date_parts or date_parts[0] is None:
                        continue

                    year = int(date_parts[0])
                    month = int(date_parts[1]) if len(date_parts) > 1 and date_parts[1] else 1
                    day = int(date_parts[2]) if len(date_parts) > 2 and date_parts[2] else 1
                    pub_date = dt.date(year, month, day).isoformat()
                    if pub_date < from_date or pub_date > until_date:
                        continue

                    container = " ".join(item.get("container-title") or [])
                    subject = " ".join(item.get("subject") or [])
                    abstract = clean_text(item.get("abstract") or "")

                    flag = get_abstract_flag(abstract)
                    content = f"{title} {abstract} {container} {subject}" if flag == "ok" else title
                    # BRIEF v2 §5 domain gate (see fetch_from_rss).
                    if not passes_domain_gate(journal_item["journal"], content):
                        continue
                    matched = match_keywords(content)

                    authors = []
                    for author in (item.get("author") or []):
                        given = author.get("given") or ""
                        family = author.get("family") or ""
                        full_name = (given + " " + family).strip()
                        if full_name:
                            authors.append(full_name)

                    paper_id = dedup_key(doi=doi, link=link, title=title)
                    if paper_id in seen:
                        continue
                    seen.add(paper_id)
                    kept += 1

                    papers.append(
                        _paper_payload(
                            paper_id=paper_id,
                            source="crossref",
                            journal=journal_item["journal"],
                            title=title,
                            date=pub_date,
                            datetime_utc=pub_date,
                            link=link,
                            doi=doi,
                            categories=matched,
                            institutions=extract_crossref_institutions(item),
                            abstract=abstract,
                            abstract_flag=flag,
                            authors=authors,
                        )
                    )

                next_cursor = (message.get("next-cursor") or "").strip()
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                time.sleep(SLEEP_BETWEEN_CALLS_SEC)
                if fetched >= 2000:
                    break
        _emit(progress_callback, f"Finished Crossref {journal_item['journal']}; fetched {fetched}, kept {kept}", stage="fetch_crossref")
    return papers


# 2026-06-14: topic-wide preprint intake from EarthArXiv (Crossref prefix
# 10.31223) and ESS Open Archive / ESSOAr (Authorea prefix 10.22541, filtered to
# essoar DOIs). OpenAlex stopped indexing both, so we read Crossref posted-content
# directly. Both venues are tier=BROAD in the registry, so passes_domain_gate
# applies the seismology filter (+ the Layer-2 LLM gate downstream) — only
# seismology/geophysics preprints survive. Watchlist authors' preprints already
# arrive via fetch_from_openalex_by_author (explicit_follow); this is the
# topic-wide stream on top of that.
PREPRINT_PREFIX_SOURCES = [
    {"prefix": "10.31223", "journal": "EarthArXiv", "doi_contains": None},
    {"prefix": "10.22541", "journal": "ESS Open Archive", "doi_contains": "essoar"},
]


def fetch_from_crossref_prefix(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    """Pull recent preprints from PREPRINT_PREFIX_SOURCES via the Crossref DOI prefix."""
    papers: List[Dict[str, Any]] = []
    if not ENABLE_CROSSREF:
        return papers
    from_date = date_from.isoformat()
    until_date = date_to.isoformat()
    for src in PREPRINT_PREFIX_SOURCES:
        prefix = src["prefix"]
        label = src["journal"]
        needle = (src.get("doi_contains") or "").lower() or None
        # Crossref prefix-scoped works endpoint + posted-date window + cursor paging.
        # Do NOT pass `cursor-max` — Crossref 400s on it (same bug that silently
        # killed the journal crossref path).
        url = f"https://api.crossref.org/prefixes/{prefix}/works"
        fetched = 0
        kept = 0
        _emit(progress_callback, f"Scanning preprint server: {label}", stage="fetch_preprint")
        cursor = "*"
        while True:
            params = {
                "filter": f"from-posted-date:{from_date},until-posted-date:{until_date}",
                "rows": 200,
                "cursor": cursor,
                "mailto": "example@example.org",
            }
            try:
                data = http_get_json(url, params)
            except Exception:
                break
            message = data.get("message") or {}
            items = message.get("items") or []
            if not items:
                break
            for item in items:
                fetched += 1
                doi = normalize_doi(item.get("DOI") or "")
                if needle and needle not in (doi or "").lower():
                    continue  # ESS Open Archive: keep only essoar DOIs out of all-Authorea
                title_list = item.get("title") or []
                title = clean_text(title_list[0] if title_list else "")
                if not title:
                    continue
                link = f"https://doi.org/{doi}" if doi else ""
                posted = item.get("posted") or item.get("published-online") or item.get("created") or {}
                date_parts = (posted.get("date-parts") or [[None]])[0]
                if not date_parts or date_parts[0] is None:
                    continue
                year = int(date_parts[0])
                month = int(date_parts[1]) if len(date_parts) > 1 and date_parts[1] else 1
                day = int(date_parts[2]) if len(date_parts) > 2 and date_parts[2] else 1
                pub_date = dt.date(year, month, day).isoformat()
                if pub_date < from_date or pub_date > until_date:
                    continue
                subject = " ".join(item.get("subject") or [])
                abstract = clean_text(item.get("abstract") or "")
                flag = get_abstract_flag(abstract)
                content = f"{title} {abstract} {subject}" if flag == "ok" else title
                if not passes_domain_gate(label, content):
                    continue  # BROAD-tier preprint server → seismology gate applies
                matched = match_keywords(content)
                authors = []
                for author in (item.get("author") or []):
                    full_name = ((author.get("given") or "") + " " + (author.get("family") or "")).strip()
                    if full_name:
                        authors.append(full_name)
                paper_id = dedup_key(doi=doi, link=link, title=title)
                if paper_id in seen:
                    continue
                seen.add(paper_id)
                kept += 1
                papers.append(
                    _paper_payload(
                        paper_id=paper_id,
                        source="crossref",
                        journal=label,
                        title=title,
                        date=pub_date,
                        datetime_utc=pub_date,
                        link=link,
                        doi=doi,
                        categories=matched,
                        institutions=extract_crossref_institutions(item),
                        abstract=abstract,
                        abstract_flag=flag,
                        authors=authors,
                    )
                )
            next_cursor = (message.get("next-cursor") or "").strip()
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)
            if fetched >= 2000:
                break
        _emit(progress_callback, f"Finished preprint {label}; fetched {fetched}, kept {kept}", stage="fetch_preprint")
    return papers


def fetch_from_eth_rc(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    if not ENABLE_ETH_RC:
        return papers

    api_key = load_eth_rc_api_key()
    if not api_key:
        _emit(progress_callback, "ETH Research Collection skipped because no API key is configured", stage="fetch_eth_rc")
        return papers

    search_url = f"{ETH_RC_BASE_URL}/discover/search/objects"
    _emit(progress_callback, f"Scanning ETH Research Collection with {len(ETH_RC_SEARCH_TERMS)} search terms", stage="fetch_eth_rc")
    for term in ETH_RC_SEARCH_TERMS:
        page = 0
        term_fetched = 0
        term_kept = 0
        while True:
            params = {
                "apikey": api_key,
                "query": term,
                "size": 50,
                "page": page,
                "sort": "dc.date.issued,DESC",
            }
            try:
                data = http_get_json(search_url, params)
            except Exception:
                break

            embedded = data.get("_embedded", {})
            search_result = embedded.get("searchResult", {})
            objects = (search_result.get("_embedded", {}) or {}).get("objects", [])
            if not objects:
                objects = data.get("results", data.get("items", []))
            if not objects:
                break

            for obj in objects:
                term_fetched += 1
                item = obj
                if isinstance(obj, dict) and "_embedded" in obj:
                    item = obj["_embedded"].get("indexableObject", obj)

                metadata = item.get("metadata", {})
                if not metadata:
                    continue

                title_meta = metadata.get("dc.title", [])
                title = clean_text(title_meta[0].get("value", "") if title_meta else "")
                if not title:
                    continue

                doi_meta = metadata.get("dc.identifier.doi", [])
                doi = normalize_doi(doi_meta[0].get("value", "") if doi_meta else "")

                date_meta = metadata.get("dc.date.issued", [])
                pub_date_str = date_meta[0].get("value", "") if date_meta else ""
                if not pub_date_str:
                    continue

                try:
                    value = pub_date_str.strip()
                    if len(value) == 4:
                        pub_date = dt.date(int(value), 1, 1)
                    elif len(value) == 7:
                        parts = value.split("-")
                        pub_date = dt.date(int(parts[0]), int(parts[1]), 1)
                    elif len(value) >= 10:
                        pub_date = dt.date.fromisoformat(value[:10])
                    else:
                        continue
                except (ValueError, IndexError):
                    continue

                if pub_date < date_from or pub_date > date_to:
                    continue

                abstract_meta = metadata.get("dc.description.abstract", [])
                abstract = clean_text(abstract_meta[0].get("value", "") if abstract_meta else "")

                authors = []
                for author in metadata.get("dc.contributor.author", []):
                    name = (author.get("value", "") or "").strip()
                    if name:
                        authors.append(name)

                handle = item.get("handle", "")
                link = ""
                if doi:
                    link = f"https://doi.org/{doi}"
                elif handle:
                    link = f"https://www.research-collection.ethz.ch/handle/{handle}"

                journal_name = ""
                for field in [
                    "dc.source",
                    "dc.relation.ispartof",
                    "dc.relation.ispartofseries",
                    "ethz.journal.title",
                ]:
                    metadata_list = metadata.get(field, [])
                    if metadata_list and isinstance(metadata_list, list):
                        value = metadata_list[0].get("value", "").strip()
                        if value:
                            journal_name = value
                            break

                if not journal_name:
                    type_meta = metadata.get("dc.type", [])
                    doc_type = type_meta[0].get("value", "").strip() if type_meta else ""
                    journal_name = f"ETH Zurich ({doc_type})" if doc_type else "ETH Zurich (unknown type)"

                flag = get_abstract_flag(abstract)
                content = f"{title} {abstract}" if flag == "ok" else title
                # BRIEF v2 §5 domain gate. ETH-RC journal labels are either a
                # real journal title (resolved to its registry tier) or an
                # ad-hoc "ETH Zurich (...)" string (unknown -> gated). The
                # domain filter is a strict superset of the old narrow gate so
                # nothing previously kept is dropped.
                if not passes_domain_gate(journal_name, content):
                    continue
                matched = match_keywords(content)

                paper_id = dedup_key(doi=doi, link=handle, title=title)
                if paper_id in seen:
                    continue
                seen.add(paper_id)
                term_kept += 1

                papers.append(
                    _paper_payload(
                        paper_id=paper_id,
                        source="eth_rc",
                        journal=journal_name,
                        title=title,
                        date=pub_date.isoformat(),
                        datetime_utc=pub_date.isoformat(),
                        link=link,
                        doi=doi,
                        categories=matched,
                        abstract=abstract,
                        abstract_flag=flag,
                        authors=authors,
                        institutions=extract_eth_rc_institutions(metadata),
                    )
                )

            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages", 1)
            if page + 1 >= total_pages:
                break
            page += 1
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)
            if term_fetched >= 200:
                break
        if term_fetched:
            _emit(progress_callback, f"Finished ETH-RC term '{term}'; fetched {term_fetched}, kept {term_kept}", stage="fetch_eth_rc")
    return papers


# ---------------------------------------------------------------------------
# arXiv category fetcher (BRIEF v2 §2 — explicit-follow intake)
# ---------------------------------------------------------------------------
#
# A tracker with an ``arxiv_category`` predicate (e.g. categories=["physics.geo-ph"])
# is an EXPLICIT follow: every returned paper bypasses the seismology domain gate
# (explicit_follow=True). The arXiv Atom API has no reliable server-side date
# filter, so we sort by submittedDate DESC and stop paging once entries fall
# before ``date_from``. The 3-second ToU throttle mirrors
# services/metadata_lookup._arxiv_throttle_wait. Returns the SAME Paper-dict
# shape as the other fetchers and is dedup-compatible via the shared ``seen`` set.

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_MIN_INTERVAL_SEC = 3.0
ARXIV_DEFAULT_CATEGORIES = ["physics.geo-ph"]
_ARXIV_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_last_arxiv_fetch_ts: List[float] = [0.0]


def _arxiv_throttle() -> None:
    """Honour arXiv ToU's 3-second min-interval per process (best-effort)."""
    now = time.time()
    delta = now - _last_arxiv_fetch_ts[0]
    if 0 < delta < ARXIV_MIN_INTERVAL_SEC:
        time.sleep(ARXIV_MIN_INTERVAL_SEC - delta)
    _last_arxiv_fetch_ts[0] = time.time()


def _parse_arxiv_datetime(value: str) -> Optional[dt.datetime]:
    """Parse an arXiv Atom timestamp (``2024-01-31T12:00:00Z``) to aware UTC."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        # Fall back to a date-only prefix if present.
        try:
            return dt.datetime.fromisoformat(value[:10]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None


def fetch_from_arxiv(
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    categories: Optional[List[str]] = None,
    progress_callback: Optional[Callable[..., None]] = None,
    max_results: int = 400,
    page_size: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch recent arXiv papers in ``categories`` published within the window.

    EXPLICIT follow (BRIEF v2 §2): results carry ``explicit_follow=True`` so
    ingest bypasses the domain gate. ``categories`` are OR-combined into the
    Atom ``search_query`` (default ``["physics.geo-ph"]``). Sorted by
    ``submittedDate`` DESC; paging stops once an entry predates ``date_from``.

    Returns the standard Paper-dict list and is dedup-compatible (the shared
    ``seen`` set is honoured). Network / parse failures are swallowed to a
    partial / empty list — never raised — matching the other fetchers.
    """
    papers: List[Dict[str, Any]] = []
    cats = [c.strip() for c in (categories or ARXIV_DEFAULT_CATEGORIES) if c and c.strip()]
    if not cats:
        return papers

    search_query = " OR ".join(f"cat:{c}" for c in cats)
    _emit(
        progress_callback,
        f"Scanning arXiv categories {', '.join(cats)} for {date_from.isoformat()}..{date_to.isoformat()}",
        stage="fetch_arxiv",
    )

    start = 0
    fetched = 0
    kept = 0
    stop = False
    while not stop and fetched < max_results:
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": min(page_size, max_results - fetched),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        _arxiv_throttle()
        try:
            response = requests.get(
                ARXIV_API_URL,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=HTTP_TIMEOUT,
                verify=(not DISABLE_SSL_VERIFY),
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            _emit(progress_callback, f"arXiv fetch stopped: {exc}", stage="fetch_arxiv")
            break

        entries = root.findall("atom:entry", _ARXIV_ATOM_NS)
        if not entries:
            break

        for entry in entries:
            fetched += 1
            published_raw = entry.findtext("atom:published", default="", namespaces=_ARXIV_ATOM_NS) or ""
            updated_raw = entry.findtext("atom:updated", default="", namespaces=_ARXIV_ATOM_NS) or ""
            pub_dt = _parse_arxiv_datetime(published_raw) or _parse_arxiv_datetime(updated_raw)
            if pub_dt is None:
                continue
            pub_date = pub_dt.date()
            if pub_date < date_from:
                # Sorted DESC — everything past here is older; stop paging.
                stop = True
                continue
            if pub_date > date_to:
                continue

            title = clean_text(entry.findtext("atom:title", default="", namespaces=_ARXIV_ATOM_NS) or "")
            abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=_ARXIV_ATOM_NS) or "")
            if not title:
                continue

            authors = [
                (a.findtext("atom:name", default="", namespaces=_ARXIV_ATOM_NS) or "").strip()
                for a in entry.findall("atom:author", _ARXIV_ATOM_NS)
            ]
            authors = [a for a in authors if a]

            # DOI when the preprint has one; else the arXiv abs page.
            doi_el = entry.find("arxiv:doi", _ARXIV_ATOM_NS)
            doi = normalize_doi((doi_el.text or "") if doi_el is not None else "")
            entry_id = (entry.findtext("atom:id", default="", namespaces=_ARXIV_ATOM_NS) or "").strip()
            link = ""
            for link_el in entry.findall("atom:link", _ARXIV_ATOM_NS):
                if link_el.attrib.get("rel") == "alternate":
                    link = link_el.attrib.get("href", "") or ""
                    break
            if not link:
                link = entry_id

            primary_cat_el = entry.find("arxiv:primary_category", _ARXIV_ATOM_NS)
            primary_cat = (
                primary_cat_el.attrib.get("term", "") if primary_cat_el is not None else ""
            ).strip()

            flag = get_abstract_flag(abstract)

            paper_id = dedup_key(doi=doi, link=entry_id or link, title=title)
            if paper_id in seen:
                continue
            seen.add(paper_id)
            kept += 1

            papers.append(
                _paper_payload(
                    paper_id=paper_id,
                    source="arxiv",
                    journal="arXiv",
                    title=title,
                    date=pub_date.isoformat(),
                    datetime_utc=pub_dt.isoformat(),
                    link=link,
                    doi=doi,
                    categories=[primary_cat] if primary_cat else None,
                    abstract=abstract,
                    abstract_flag=flag,
                    authors=authors,
                    institutions=[],
                    explicit_follow=True,
                    arxiv_category=primary_cat or None,
                )
            )

        if len(entries) < params["max_results"]:
            break
        start += len(entries)

    _emit(progress_callback, f"Finished arXiv; fetched {fetched}, kept {kept}", stage="fetch_arxiv")
    return papers


# ---------------------------------------------------------------------------
# OpenAlex author fetcher (BRIEF v2 §2 — explicit-follow intake)
# ---------------------------------------------------------------------------
#
# A tracker with an ``author`` predicate is an EXPLICIT follow. We resolve the
# author via OpenAlex — an OpenAlex author id ("A123…", or a full
# "https://openalex.org/A123…" URL) filters exactly on ``author.id``; anything
# else is treated as a free-text name and matched with
# ``raw_author_name.search``. Every returned paper bypasses the domain gate
# (explicit_follow=True) and shares the OpenAlex Paper-dict shape so dedup with
# the journal-ISSN OpenAlex path is automatic via the shared ``seen`` set.

_OPENALEX_AUTHOR_ID_RE = re.compile(r"^A\d{4,}$", re.IGNORECASE)


def _normalize_openalex_author_filter(author_id_or_name: str) -> Tuple[str, str]:
    """Return ``(filter_key, value)`` for the OpenAlex works filter.

    ``("author.id", "A123…")`` for an id (bare or a full openalex.org URL),
    else ``("raw_author_name.search", "<name>")`` for a free-text name.
    """
    raw = (author_id_or_name or "").strip()
    candidate = raw
    if "openalex.org/" in candidate.lower():
        candidate = candidate.rstrip("/").rsplit("/", 1)[-1]
    if _OPENALEX_AUTHOR_ID_RE.match(candidate):
        return ("author.id", candidate.upper())
    return ("raw_author_name.search", raw)


def _recover_venue_from_openalex(work: Dict[str, Any], primary_location: Dict[str, Any], doi: str) -> str:
    """Best-effort venue label for an OpenAlex work (2026-07-05 fix).

    OpenAlex frequently indexes conference abstracts (esp. EGU General Assembly
    via Copernicus) and some preprints with NO ``primary_location.source``. The
    old code then stored the journal as ``"Others"`` — wrong. Recover a real
    label: primary source → any other location's source → host organisation → a
    DOI-prefix/pattern map for known conference/preprint publishers. Returns ""
    only when the venue is genuinely unknown (caller keeps the "Others" default).
    """
    src = primary_location.get("source") if isinstance(primary_location, dict) else None
    if isinstance(src, dict):
        name = (src.get("display_name") or "").strip()
        if name:
            return name
    for loc in (work.get("locations") or []):
        if isinstance(loc, dict):
            s = loc.get("source")
            if isinstance(s, dict):
                name = (s.get("display_name") or "").strip()
                if name:
                    return name
    if isinstance(src, dict):
        host = (src.get("host_organization_name") or "").strip()
        if host:
            return host
    d = (doi or "").lower()
    if d:
        if "egusphere-egu" in d or "/egu" in d:
            return "EGU General Assembly"
        if d.startswith("10.5194"):
            return "Copernicus"
        if d.startswith("10.31223"):
            return "EarthArXiv"
        if d.startswith("10.22541"):
            return "ESS Open Archive"
    return ""


def fetch_from_openalex_by_author(
    author_id_or_name: str,
    date_from: dt.date,
    date_to: dt.date,
    seen: set[str],
    progress_callback: Optional[Callable[..., None]] = None,
    max_results: int = 600,
) -> List[Dict[str, Any]]:
    """Fetch an author's works in the window from OpenAlex (explicit follow).

    Accepts an OpenAlex author id (``A123…`` or its full URL) or a free-text
    name. Results carry ``explicit_follow=True`` (BRIEF v2 §2) so ingest bypasses
    the seismology domain gate. Same Paper-dict shape + dedup as
    :func:`fetch_from_openalex`. NEVER raises — a network error yields whatever
    was collected so far.
    """
    papers: List[Dict[str, Any]] = []
    if not (author_id_or_name or "").strip():
        return papers

    base_url = "https://api.openalex.org/works"
    from_date = date_from.isoformat()
    until_date = date_to.isoformat()
    filter_key, filter_value = _normalize_openalex_author_filter(author_id_or_name)
    filter_string = (
        f"from_publication_date:{from_date},to_publication_date:{until_date},"
        f"{filter_key}:{filter_value}"
    )

    _emit(
        progress_callback,
        f"Scanning OpenAlex author {filter_value} ({filter_key})",
        stage="fetch_openalex_author",
    )

    cursor = "*"
    fetched = 0
    kept = 0
    while True:
        params = {
            "filter": filter_string,
            "per-page": 200,
            "cursor": cursor,
            "mailto": "example@example.org",
        }
        try:
            data = http_get_json(base_url, params)
        except Exception as exc:
            _emit(progress_callback, f"OpenAlex author fetch stopped: {exc}", stage="fetch_openalex_author")
            break

        results = data.get("results", []) or []
        if not results:
            break

        for work in results:
            fetched += 1
            title = clean_text(work.get("title", "") or "")
            doi = normalize_doi(work.get("doi", "") or "")

            link = ""
            primary_location = work.get("primary_location") or {}
            if isinstance(primary_location, dict):
                link = primary_location.get("landing_page_url") or primary_location.get("pdf_url") or ""
            if not link and doi:
                link = f"https://doi.org/{doi}"

            pub_date = (work.get("publication_date") or "").strip()
            if not pub_date:
                continue
            if pub_date < from_date or pub_date > until_date:
                continue
            if not title:
                continue

            # Resolve the venue label. OpenAlex often has NO source for conference
            # abstracts (EGU General Assembly) / some preprints — recover a real
            # label from other locations / host org / DOI prefix instead of the
            # old hard "Others" default (2026-07-05 fix).
            journal_label = _recover_venue_from_openalex(work, primary_location, doi)

            abstract = clean_text(openalex_abstract_from_inverted_index(work.get("abstract_inverted_index")))
            flag = get_abstract_flag(abstract)
            matched = match_keywords(f"{title} {abstract}" if flag == "ok" else title)

            authors = []
            for authorship in (work.get("authorships") or []):
                author_name = (authorship.get("author") or {}).get("display_name")
                if author_name:
                    authors.append(author_name)

            paper_id = dedup_key(doi=doi, link=link, title=title)
            if paper_id in seen:
                continue
            seen.add(paper_id)
            kept += 1

            papers.append(
                _paper_payload(
                    paper_id=paper_id,
                    source="openalex",
                    journal=journal_label or "Others",
                    title=title,
                    date=pub_date,
                    datetime_utc=pub_date,
                    link=link,
                    doi=doi,
                    categories=matched,
                    institutions=extract_openalex_institutions(work),
                    keywords=extract_openalex_keywords(work),
                    abstract=abstract,
                    abstract_flag=flag,
                    authors=authors,
                    explicit_follow=True,
                )
            )

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(SLEEP_BETWEEN_CALLS_SEC)
        if fetched >= max_results:
            break

    _emit(progress_callback, f"Finished OpenAlex author {filter_value}; fetched {fetched}, kept {kept}", stage="fetch_openalex_author")
    return papers


# ---------------------------------------------------------------------------
# Predicate -> source plan (BRIEF v2 §2)
# ---------------------------------------------------------------------------


def derive_sources_from_predicates(project: Any) -> Dict[str, Any]:
    """Inspect a tracker's predicates and return WHAT to fetch for it.

    Maps the SOURCE predicate types (BRIEF v2 §2) to concrete intake actions:

    * ``author``         -> an OpenAlex author feed per name/id.
    * ``arxiv_category`` -> an arXiv category fetch (categories pooled).
    * ``journal``        -> ensure each journal's ISSN(s) are scanned (the
      abbreviations the predicate stores are resolved back to registry ISSNs so
      the OpenAlex/Crossref intake covers them).

    Returns a plan dict::

        {
            "explicit_follow": bool,        # True iff EVERY predicate is a source
                                            # type (a pure explicit-follow tracker)
            "authors":     [str, ...],      # OpenAlex author names/ids to feed
            "arxiv_categories": [str, ...], # arXiv categories to fetch
            "journals":    [str, ...],      # followed journal abbreviations (UPPER-cased,
                                            # matching JournalPredicate / create_project)
            "issns":       [str, ...],      # registry ISSNs to ensure are scanned
        }

    The matcher still re-checks each fetched paper against the predicates; this
    only decides intake. ``project`` may be an ORM ``Project``, a dict with a
    ``config`` key, or a raw config dict (mirrors ``predicates._config_of``).
    NEVER raises; an empty / non-source config yields empty lists.
    """
    config = _project_config(project)

    # Lazy import keeps this module import-safe even if predicates ever grows a
    # heavier dependency; predicates itself is stdlib-only today.
    from services.predicates import (
        SOURCE_PREDICATE_TYPES,
        normalize_config as _normalize_config,
    )

    plan: Dict[str, Any] = {
        "explicit_follow": False,
        "authors": [],
        "arxiv_categories": [],
        "journals": [],
        "issns": [],
    }

    # Drive everything off the NORMALIZED specs (not built Predicate objects):
    # ``arxiv_category`` is in SOURCE_PREDICATE_TYPES but has no builder in
    # predicates.PREDICATE_REGISTRY yet, so ``build_predicates`` would silently
    # drop it (and ``is_source_only_config`` would miss an arxiv-only follow).
    # The normalized spec list preserves it, keeping this forward-compatible.
    specs = [s for s in _normalize_config(config).get("predicates", []) if isinstance(s, dict)]
    if not specs:
        return plan

    spec_types = [str(s.get("type") or "").strip().lower() for s in specs]
    # A pure explicit-follow tracker: EVERY predicate is a source type. Such a
    # tracker (journal-only / author-only / arxiv-only) bypasses the domain gate
    # at intake (BRIEF v2 §2).
    plan["explicit_follow"] = bool(spec_types) and all(
        t in SOURCE_PREDICATE_TYPES for t in spec_types
    )

    seen_authors: set[str] = set()
    seen_cats: set[str] = set()
    seen_journals: set[str] = set()
    seen_issns: set[str] = set()

    # Build an abbrev/title -> ISSN map from the registry once so journal
    # predicates (which store abbreviations) expand to the ISSNs intake scans.
    from services.journal_registry import JOURNALS as _REG_JOURNALS

    abbrev_to_issns: Dict[str, List[str]] = {}
    title_to_issns: Dict[str, List[str]] = {}
    for entry in _REG_JOURNALS:
        issns = [str(i) for i in (entry.get("issn") or []) if i]
        if not issns:
            continue
        abbrev_key = str(entry.get("abbrev") or "").strip().upper()
        if abbrev_key:
            abbrev_to_issns.setdefault(abbrev_key, issns)
        title_key = str(entry.get("name") or "").strip().upper()
        if title_key:
            title_to_issns.setdefault(title_key, issns)

    def _spec_terms(spec: Dict[str, Any]) -> List[str]:
        for key in ("any_of", "values", "value", "terms", "items", "categories", "names"):
            if spec.get(key) is not None:
                val = spec[key]
                if isinstance(val, str):
                    return [val]
                if isinstance(val, (list, tuple, set)):
                    return [str(x) for x in val if x is not None]
                return [str(val)]
        return []

    for spec in specs:
        ptype = str(spec.get("type") or "").strip().lower()
        if ptype == "author":
            # Prefer OpenAlex author IDs (precise: avoids "Li/Wang" name
            # collisions in the by-author fetch); fall back to free-text names.
            author_terms = [str(x) for x in (spec.get("author_ids") or []) if str(x).strip()] or _spec_terms(spec)
            for name in author_terms:
                key = name.strip()
                if key and key.lower() not in seen_authors:
                    seen_authors.add(key.lower())
                    plan["authors"].append(key)
        elif ptype == "arxiv_category":
            for cat in _spec_terms(spec):
                key = cat.strip()
                if key and key.lower() not in seen_cats:
                    seen_cats.add(key.lower())
                    plan["arxiv_categories"].append(key)
        elif ptype == "journal":
            for abbrev in _spec_terms(spec):
                key = abbrev.strip()
                if not key:
                    continue
                upper = key.upper()
                if upper not in seen_journals:
                    seen_journals.add(upper)
                    plan["journals"].append(upper)
                for issn in abbrev_to_issns.get(upper, []) + title_to_issns.get(upper, []):
                    if issn not in seen_issns:
                        seen_issns.add(issn)
                        plan["issns"].append(issn)

    return plan


def _project_config(project: Any) -> Dict[str, Any]:
    """Extract a tracker config dict from an ORM Project / dict / raw config."""
    if isinstance(project, dict):
        if "predicates" in project or "match" in project or "query" in project:
            return project
        cfg = project.get("config")
        return cfg if isinstance(cfg, dict) else project
    cfg = getattr(project, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _fetch_tracker_sources(
    db_session,
    date_from: dt.date,
    date_to: dt.date,
    seen: set,
    *,
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    """Tracker-driven targeted intake (the previously-missing wiring, 2026-06-13).

    For every active tracker carrying author / arxiv_category SOURCE predicates,
    go FETCH those authors'/categories' recent works (incl. preprints) so author
    monitoring actually PULLS new output instead of only filtering whatever the
    journal feeds happened to bring in. Results join the shared ``seen`` dedup
    set; author/arXiv papers are tagged ``explicit_follow`` by the fetchers so
    they bypass the domain + LLM gates downstream. NEVER raises — one failed
    source is logged and skipped. See _audit_2026-06-11/06-DATAFLOW-AND-REFOCUS.md.
    """
    from models import Project

    out: List[Dict[str, Any]] = []
    try:
        trackers = (
            db_session.query(Project)
            .filter(Project.kind == "tracker", Project.archived_at.is_(None))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        _emit(progress_callback, f"tracker-source intake skipped: {exc}", stage="fetch_author")
        return out

    done_authors: set = set()
    done_cats: set = set()
    for proj in trackers:
        try:
            plan = derive_sources_from_predicates(proj)
        except Exception:
            continue
        for author in plan.get("authors", []):
            key = (author or "").strip().lower()
            if not key or key in done_authors:
                continue
            done_authors.add(key)
            try:
                out.extend(
                    fetch_from_openalex_by_author(
                        author, date_from, date_to, seen, progress_callback=progress_callback
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _emit(progress_callback, f"author fetch failed for {author}: {exc}", stage="fetch_openalex_author")
        new_cats = [c for c in plan.get("arxiv_categories", []) if c and c.lower() not in done_cats]
        for c in new_cats:
            done_cats.add(c.lower())
        if new_cats:
            try:
                out.extend(
                    fetch_from_arxiv(date_from, date_to, seen, categories=new_cats, progress_callback=progress_callback)
                )
            except Exception as exc:  # noqa: BLE001
                _emit(progress_callback, f"arxiv fetch failed: {exc}", stage="fetch_arxiv")

    if out:
        _emit(
            progress_callback,
            f"Tracker-driven intake: pulled {len(out)} paper(s) from {len(done_authors)} author(s) + {len(done_cats)} arXiv cat(s)",
            stage="fetch_author",
        )
    return out


def fetch_and_store_new_papers(
    db_session,
    days_lookback: int = DAYS_LOOKBACK,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    progress_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, int]:
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    resolved_date_to = date_to or today_utc
    resolved_date_from = date_from or (resolved_date_to - dt.timedelta(days=days_lookback))
    if resolved_date_from > resolved_date_to:
        raise ValueError("date_from must be earlier than or equal to date_to")

    if date_from or date_to:
        range_label = f"{resolved_date_from.isoformat()} to {resolved_date_to.isoformat()}"
        _emit(progress_callback, f"Starting fetch for papers published from {range_label}", stage="fetch_start")
    else:
        _emit(
            progress_callback,
            f"Starting fetch for papers from the last {days_lookback} days; range={resolved_date_from.isoformat()} to {resolved_date_to.isoformat()}",
            stage="fetch_start",
        )

    seen: set[str] = set()
    papers_all: List[Dict[str, Any]] = []
    # P1-1/P1-2 (2026-06-11): order = strongest source first. Since dedup_key is
    # now source-independent, the FIRST fetcher to emit a given DOI wins the
    # shared `seen` race and owns that row's metadata; later (weaker) sources are
    # skipped that run. Running crossref/openalex (rich authors+institutions)
    # before rss (authors=[]) means the authoritative row wins from the start;
    # P1-2's field-level merge then upgrades it on later runs.
    # See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
    papers_all.extend(fetch_from_crossref(resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback))
    papers_all.extend(fetch_from_openalex(resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback))
    papers_all.extend(fetch_from_eth_rc(resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback))
    papers_all.extend(fetch_from_rss(resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback))
    # 2026-06-14: topic-wide preprint servers (EarthArXiv / ESS Open Archive) via
    # Crossref DOI-prefix — seismology-gated (BROAD tier). See PREPRINT_PREFIX_SOURCES.
    papers_all.extend(fetch_from_crossref_prefix(resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback))

    # Tracker-driven targeted intake: pull watchlist authors' / followed arXiv
    # categories' new output (incl. preprints) — the main-line author/preprint
    # monitoring. Joins the same `seen` dedup; explicit_follow bypasses gates.
    papers_all.extend(
        _fetch_tracker_sources(db_session, resolved_date_from, resolved_date_to, seen, progress_callback=progress_callback)
    )

    papers_all.sort(key=lambda item: item.get("datetime_utc", item.get("date", "")), reverse=True)

    # Layer-2 domain gate: drop off-domain broad-tier papers BEFORE we embed /
    # store them (no point spending an embedding on a bearing-fault paper). Pure-
    # tier venues are trusted; degrades gracefully (keeps all on failure/disable).
    _keep_ids = llm_domain_keep_ids(papers_all, progress_callback=progress_callback)
    if len(_keep_ids) < len(papers_all):
        papers_all = [p for p in papers_all if p["id"] in _keep_ids]

    _emit(progress_callback, f"Fetch phase complete; {len(papers_all)} new papers will be written to SQLite", stage="fetch_store")

    created = 0
    updated = 0
    # BRIEF v2 §3: collect every paper we touch so we can compute the CHEAP
    # title+abstract embedding for the batch right after the write commit (one
    # batched embedding call, no PDF, no Gemini Pro). The list dedups by id so a
    # paper that appears from two sources is embedded once.
    touched: Dict[str, Paper] = {}
    # H4 fix (2026-06-30) — reconcile-by-DOI index. dedup_key() keys a DOI-less
    # entry by title and a DOI-bearing entry by DOI, so the SAME paper arriving
    # first without a DOI (RSS/arXiv) then later with one (OpenAlex/Crossref, or
    # preprint→published) produced TWO rows. We now look a would-be-new DOI-bearing
    # row up by its DOI (case-insensitively — DOIs are case-insensitive) against
    # both this run's pending rows and the DB, and merge onto the match instead of
    # inserting a duplicate. Keyed by lowercased normalized DOI -> Paper.
    by_doi: Dict[str, Paper] = {}
    for paper_data in papers_all:
        pid = paper_data["id"]
        _incoming_doi = normalize_doi(paper_data.get("doi"))
        _doi_lc = _incoming_doi.lower() if _incoming_doi else ""
        if pid in touched:
            # Same id already handled earlier this run (e.g. two sources for one
            # DOI both reached here). Merge onto that pending row via the same
            # source-of-truth rules below instead of re-INSERTing — a second
            # add() would trip the papers.id PK constraint (db_session.get does
            # NOT see an un-flushed pending object). This also gives within-run
            # cross-source fill-empty merge. See 05-CLEANUP-EXECUTED.md.
            paper = touched[pid]
            is_new = False
        else:
            paper = db_session.get(Paper, pid)
            if paper is None and _doi_lc:
                # Would be a new row under its (title/link) dedup_key, but a row
                # with the same DOI may already exist under a different key. Prefer
                # a pending row touched earlier this run, else an existing DB row.
                paper = by_doi.get(_doi_lc) or (
                    db_session.query(Paper)
                    .filter(func.lower(Paper.doi) == _doi_lc)
                    .first()
                )
            is_new = paper is None
            if is_new:
                paper = Paper(id=pid)
                db_session.add(paper)
                created += 1
            else:
                updated += 1
            touched[pid] = paper

        # P1-2 (2026-06-11) field-level source-of-truth. New rows are written in
        # full. For an EXISTING row, a weaker source (e.g. the 6h×30d periodic
        # RSS re-fetch) must not clobber metadata a stronger source already wrote
        # — otherwise analyze-coerced topics / backfilled abstracts get reset
        # every cycle. Weaker incoming only fills genuinely empty fields.
        # See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
        strong = is_new or _source_rank(paper_data["source"]) >= _source_rank(paper.source)

        def _set(attr, value):
            # Merge rule (per adversarial review): fill an empty field with
            # anything; a STRONG source may update a non-empty field only with a
            # non-empty value; never let an empty/weaker value clobber existing
            # non-empty data. On a new row every field is empty, so all get set.
            cur = getattr(paper, attr, None)
            if _is_empty(cur):
                setattr(paper, attr, value)
            elif strong and not _is_empty(value):
                setattr(paper, attr, value)

        if strong:
            paper.source = paper_data["source"]
        _set("title", paper_data["title"])
        _set("journal", normalize_journal(paper_data.get("journal")))
        _set("institutions", normalize_institutions(paper_data.get("institutions")))
        _set("keywords", list(paper_data.get("keywords") or []))
        _set("authors", paper_data.get("authors") or [])
        _set("published_date", paper_data.get("date"))
        _set("datetime_utc", paper_data.get("datetime_utc"))
        _set("link", paper_data.get("link"))

        # abstract + its flag move together so an old abstract never keeps a
        # stale flag. Same merge rule as _set: a strong source may replace it
        # only with a non-empty abstract; an empty/short re-fetch never wipes a
        # full one (e.g. OpenAlex re-fetch with no abstract over an RSS full one).
        _incoming_abs = paper_data.get("abstract")
        if _is_empty(paper.abstract) or (strong and not _is_empty(_incoming_abs)):
            paper.abstract = _incoming_abs
            paper.abstract_flag = paper_data.get("abstract_flag") or "missing"

        # topics: once an AI analysis exists it OWNS topics (coerce_topics_to_preset
        # writes paper.topics), so fetch must never reset them; otherwise treat
        # topics like the other metadata fields.
        if is_new:
            paper.topics = normalize_topics(paper_data.get("categories"))
        elif paper.ai_analysis is None:
            _set("topics", normalize_topics(paper_data.get("categories")))

        # doi: fill-empty only — never overwrite a DOI we already have.
        if is_new or _is_empty(paper.doi):
            paper.doi = normalize_doi(paper_data.get("doi"))

        # H4 — index by DOI so a later same-DOI entry this run reconciles onto
        # this row (pending or existing) instead of inserting a duplicate.
        if paper.doi:
            by_doi[paper.doi.lower()] = paper

        saved_total = created + updated
        if saved_total % 10 == 0:
            _emit(
                progress_callback,
                f"Saved {saved_total}/{len(papers_all)} papers to SQLite",
                stage="fetch_store",
                created=created,
                updated=updated,
                fetched=len(papers_all),
            )

    db_session.commit()
    _emit(progress_callback, f"Fetch write completed; created {created} and updated {updated} papers", stage="fetch_done", created=created, updated=updated, fetched=len(papers_all))

    # BRIEF v2 §3 — compute the CHEAP title+abstract embedding at intake. This is
    # the ONLY model call the fetch path makes; the expensive Gemini Pro analysis
    # stays lazy (BRIEF v2 §6, run only on tracker match or open). The helper uses
    # services/embedding_service.py exclusively, never opens a PDF, and swallows
    # missing-key / quota / network failures to leave a paper embedding-less
    # rather than raise — so intake never fails on an embedding error. Imported
    # lazily to keep fetch_service import-safe (analyze_service pulls in genai).
    embedded = 0
    try:
        from services.analyze_service import embed_papers_batch

        embedded = embed_papers_batch(
            # H4 — dedup by row id: with reconcile-by-DOI two different dedup_keys
            # (title-keyed + doi-keyed) can now map to the SAME Paper this run, so
            # touched.values() may hold it twice. Embed each row at most once.
            list({p.id: p for p in touched.values()}.values()),
            db_session,
            commit=True,
            progress_callback=progress_callback,
        )
    except Exception:  # noqa: BLE001 — embedding is best-effort; intake must survive.
        logger.warning("fetch_and_store_new_papers: embedding pass failed; papers persist unembedded", exc_info=True)

    return {
        "created": created,
        "updated": updated,
        "fetched": len(papers_all),
        "embedded": embedded,
        "date_from": resolved_date_from.isoformat(),
        "date_to": resolved_date_to.isoformat(),
    }
