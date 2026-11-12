"""
Canonical topic vocabulary and rename mapping.

Extracted from analyze_service.py to avoid pulling in google-genai at DB init
time.  db.py (and anything else that only needs the raw data) should import
from here instead of from services.analyze_service.

────────────────────────────────────────────────────────────────────────────
v3 faceted taxonomy (Stage A — DEFINITIONS ONLY, zero behavior change)
────────────────────────────────────────────────────────────────────────────
The project is moving from a single flat ``COS_TOPICS`` list to a 5-facet
multi-label taxonomy:

    primary_topic (multi, ≤3) × method × data_type × target × paper_role

Stage B has flipped ``COS_TOPICS = PRIMARY_TOPICS`` (assigned at the BOTTOM of
this module, once PRIMARY_TOPICS exists). The legacy 7 survive only as keys in
``PRIMARY_TOPIC_RENAMES`` for the Stage D DB migration.

Stage B flips ``COS_TOPICS = PRIMARY_TOPICS``, rewrites the prompt to emit the
full faceted object, updates the coercion maps, and adds the ``facets`` storage
column — atomically and with tests. Stage D wires ``PRIMARY_TOPIC_RENAMES`` /
``PRIMARY_TOPIC_DROPS`` into the DB migration to rewrite legacy labels.
"""

# ── Active flat vocabulary (Stage B: aliases the v3 PRIMARY_TOPICS) ───────────
# ``COS_TOPICS`` is the name every consumer (analyze_service, db, predicates,
# the index.html mirror, tests) imports. As of Stage B it IS the 19-topic v3
# vocab — the assignment lives at the BOTTOM of this module, once PRIMARY_TOPICS
# exists. The legacy 7 survive only as keys in PRIMARY_TOPIC_RENAMES.

# Topics that have been renamed in code. Map of {legacy_value: current_value}.
# The init_db() migration rewrites these in stored JSON fields so legacy rows
# match the current vocabulary. Add new entries here whenever a topic label
# changes — the migration is idempotent.
COS_TOPIC_RENAMES: dict[str, str] = {
    "Cyrosphere Research": "Cryosphere Research",
}


# ─────────────────────────────────────────────────────────────────────────────
# v3 FACETED VOCABULARY  (defined now; activated in Stage B)
# ─────────────────────────────────────────────────────────────────────────────

# A · primary_topic — what the paper is mainly about. Multi-select, ≤3 per paper.
PRIMARY_TOPICS: list[str] = [
    "Full-waveform inversion & adjoint tomography",
    "Seismic tomography (body & surface wave)",
    "Ambient-noise seismology & interferometry",
    "Receiver functions & discontinuity imaging",
    "Numerical wave propagation & HPC",
    "Seismic anisotropy",
    "Scattering, attenuation & coda (Q)",
    "Earthquake source & rupture dynamics",
    "Fault mechanics & slow earthquakes",
    "Earthquake detection, location & catalogs",
    "Machine learning in seismology",
    "Bayesian inversion & uncertainty quantification",
    "Distributed acoustic sensing & fibre-optic seismology",
    "Array, ocean-bottom & instrumentation",
    "Crust, lithosphere & mantle structure / geodynamics",
    "Seismic hazard & strong ground motion",
    "Induced & reservoir seismicity",
    "Volcano, cryosphere, environmental & planetary seismology",
    "Inverse theory & inverse problems",
    # Escape hatch: papers that pass the loose broad-journal domain gate but are
    # not seismology/geophysics at all (mega-journal noise). The classifier uses
    # this as the SOLE primary topic instead of forcing a real seismology label.
    "Off-topic / not seismology",
]

# B · method — techniques used. Multi-select, 0–3.
METHODS: list[str] = [
    "FWI / adjoint",
    "Travel-time / ray tomography",
    "Ambient-noise cross-correlation",
    "Receiver functions",
    "Spectral-element / FD / DG modeling",
    "Normal-mode / surface-wave dispersion",
    "Beamforming / array processing",
    "Matched-filter / template matching",
    "Machine / deep learning",
    "Bayesian / probabilistic inversion",
    "Moment-tensor / source inversion",
    "Back-projection",
    "Interferometry / coda",
    "Empirical / statistical (GMPE, ETAS)",
]

# C · data_type — what data the paper uses. Multi-select, 0–3.
DATA_TYPES: list[str] = [
    "Earthquake waveforms (teleseismic/regional/local)",
    "Ambient noise",
    "DAS / fibre-optic",
    "Ocean-bottom (OBS)",
    "Strong-motion",
    "Normal modes / free oscillations",
    "GNSS / InSAR / geodesy",
    "Active-source / exploration",
    "Laboratory (rock physics)",
    "Synthetic / simulated",
]

# D · target — object / spatial scale. Multi-select, 0–3.
TARGETS: list[str] = [
    "Global mantle",
    "Whole Earth / core",
    "Crust & lithosphere",
    "Upper mantle / asthenosphere",
    "Subduction zone / slab",
    "Fault zone",
    "Volcano / magmatic system",
    "Geothermal / reservoir",
    "Glacier / cryosphere",
    "Near-surface / engineering",
    "Planetary (Mars/Moon)",
    "Ocean / marine",
]

# E · paper_role — kind of contribution. Single-select.
PAPER_ROLES: list[str] = [
    "Theory",
    "Method / algorithm",
    "Dataset / observation",
    "Application / case study",
    "Review",
    "Software / tool",
]

# The facet registry the classifier / UI / trackers consume in Stage B+.
FACETS: dict[str, list[str]] = {
    "primary_topic": PRIMARY_TOPICS,
    "method": METHODS,
    "data_type": DATA_TYPES,
    "target": TARGETS,
    "paper_role": PAPER_ROLES,
}

# Cap on primary_topic labels per paper (multi-select).
MAX_PRIMARY_TOPICS: int = 3

# Legacy flat label (old COS_TOPICS + the 6 index.html "ghost" long aliases)
# → new primary_topic. Consumed by the Stage D DB migration to rewrite stored
# paper.topics / topic_matches. NOT yet wired in (Stage A is definitions only).
PRIMARY_TOPIC_RENAMES: dict[str, str] = {
    "Full-waveform inversion": "Full-waveform inversion & adjoint tomography",
    "Full-waveform inversion (FWI) & Adjoint Imaging": "Full-waveform inversion & adjoint tomography",
    "Fibre-optic seismology": "Distributed acoustic sensing & fibre-optic seismology",
    "Fibre-optic seismology / DAS": "Distributed acoustic sensing & fibre-optic seismology",
    "Cryosphere Research": "Volcano, cryosphere, environmental & planetary seismology",
    "Cyrosphere Research": "Volcano, cryosphere, environmental & planetary seismology",
    "Cryosphere / Cryoseismology": "Volcano, cryosphere, environmental & planetary seismology",
    "Numerical wave propagation and HPC": "Numerical wave propagation & HPC",
    "Numerical wave propagation (seismic) & Large-scale simulation": "Numerical wave propagation & HPC",
    "Ambient noise seismology": "Ambient-noise seismology & interferometry",
    "Ambient noise seismology & Interferometry": "Ambient-noise seismology & interferometry",
    "Inverse theory": "Inverse theory & inverse problems",
    "Inverse theory & Uncertainty quantification": "Inverse theory & inverse problems",
}

# Legacy labels that have NO new home and are dropped at migration (Stage D).
PRIMARY_TOPIC_DROPS: set[str] = {
    "Medical ultrasound imaging",
}


# ─────────────────────────────────────────────────────────────────────────────
# Stage B flip — the canonical flat list IS the v3 primary-topic vocab.
# Defined last so PRIMARY_TOPICS already exists. Every `from services.cos_topics
# import COS_TOPICS` now gets the 19-topic list.
# ─────────────────────────────────────────────────────────────────────────────
COS_TOPICS: list[str] = PRIMARY_TOPICS
