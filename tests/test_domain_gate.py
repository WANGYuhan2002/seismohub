"""Domain gate tests (BRIEF v2 §5).

``services/fetch_service`` runs intake through a single domain gate keyed off
the journal registry:

* All venues (pure and broad) now pass ``in_seismic_domain`` (2026-06-14);
  only ``explicit_follow`` bypasses. The ``tier`` is retained for display /
  tracking only and no longer changes the intake decision.
* Every venue -> admit iff the WIDE seismology/geophysics ``in_seismic_domain``
  net matches (a strict superset of the old narrow 7-topic regex), with the
  medical/publisher EXCLUDE_PATTERNS still vetoing.
* unknown venue (not in the registry) -> treated the same as the gated pool.

These functions are pure (regex + the registry's tier map), so the tests need
no DB / no network.
"""

from __future__ import annotations

import pytest

from services import fetch_service as fs
from services.journal_registry import BROAD, PURE


# Off-topic text that the broad domain net should NOT admit (no seismic /
# geophysical token anywhere).
OFF_TOPIC = "A study of consumer purchasing habits in retail supermarkets."

# On-topic text the broad net admits (contains 'earthquake' / 'rupture').
ON_TOPIC = "Rupture dynamics of the 2023 earthquake from seismic waveforms."


# ---------------------------------------------------------------------------
# tier_for_label
# ---------------------------------------------------------------------------


def test_tier_for_label_resolves_historical_fetch_labels():
    """fetch_service's own labels (which differ from canonical titles) resolve
    to the right tier via the label->tier map."""
    assert fs.tier_for_label("EPSL") == PURE
    assert fs.tier_for_label("BSSA") == PURE
    assert fs.tier_for_label("GRL") == BROAD
    assert fs.tier_for_label("JGR: Solid Earth") == BROAD
    assert fs.tier_for_label("Geophysics (SEG)") == PURE
    assert fs.tier_for_label("GJI (Current Issue)") == PURE


def test_tier_for_label_resolves_canonical_titles():
    assert fs.tier_for_label("Bulletin of the Seismological Society of America") == PURE
    assert fs.tier_for_label("Nature") == BROAD


def test_tier_for_label_unknown_or_blank_is_none():
    assert fs.tier_for_label("ETH Zurich (Research Collection)") is None
    assert fs.tier_for_label("") is None
    assert fs.tier_for_label(None) is None


# ---------------------------------------------------------------------------
# in_seismic_domain  (the wide broad-venue net)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Rupture dynamics of a large earthquake.",
        "Distributed acoustic sensing of slow slip on the megathrust.",
        "Ambient noise tomography of the crust and upper mantle.",
        "Full-waveform inversion of teleseismic body waves.",
        "Volcanic tremor preceding the eruption.",
        "InSAR and GNSS constraints on subduction-zone coupling.",
        "Seismic hazard and tsunami risk assessment.",
    ],
)
def test_in_seismic_domain_accepts_on_topic(text: str):
    assert fs.in_seismic_domain(text) is True


@pytest.mark.parametrize(
    "text",
    [
        OFF_TOPIC,
        "A new recipe for sourdough bread fermentation.",
        "Macroeconomic effects of monetary policy on inflation.",
        "",
    ],
)
def test_in_seismic_domain_rejects_off_topic(text: str):
    assert fs.in_seismic_domain(text) is False


def test_in_seismic_domain_excludes_medical_even_with_seismic_word():
    """EXCLUDE_PATTERNS (cancer/tumour/cardiac/seizure) veto even when a
    seismic-ish token is present (medical FWI/ultrasound bleed-through)."""
    text = "Full-waveform inversion for breast cancer ultrasound tomography."
    assert fs.in_seismic_domain(text) is False


def test_in_seismic_domain_rejects_bare_generic_method():
    """2026-06-12 precision rework: the old gate was a loose superset of the
    focused KEYWORDS regex, so a bare generic method term ('Hamiltonian Monte
    Carlo sampling of the posterior') auto-passed with NO seismology context.
    That over-admission (along with bare 'fault'/'magnitude'/'rupture') is the
    exact leak we closed: a generic method phrase with no seismology term is now
    REJECTED. With seismology context it passes again (see next test)."""
    assert fs.in_seismic_domain("Hamiltonian Monte Carlo sampling of the posterior.") is False
    # Same method, now anchored in seismology -> admitted (earthquake = strong-core).
    assert fs.in_seismic_domain(
        "Hamiltonian Monte Carlo full-waveform inversion of earthquake data."
    ) is True


# ---------------------------------------------------------------------------
# passes_domain_gate  (the tier-aware intake gate)
# ---------------------------------------------------------------------------


def test_pure_venue_now_gated_off_topic():
    """2026-06-14: pure venues no longer bypass the gate. Off-topic text is
    rejected even for a pure venue (only explicit_follow bypasses; tier is
    display/tracking only)."""
    assert fs.passes_domain_gate("BSSA", OFF_TOPIC) is False
    assert fs.passes_domain_gate("EPSL", OFF_TOPIC) is False
    assert fs.passes_domain_gate("Seismica", "literally anything") is False


def test_pure_venue_now_gated_excludes():
    """A pure venue is now subject to the same domain gate, so an EXCLUDE term
    (with no seismic anchor) is rejected just like any other venue."""
    assert fs.passes_domain_gate("BSSA", "cardiac seizure tumour cancer") is False


def test_broad_venue_is_gated_on_domain():
    """tier=broad -> admit ONLY when in_seismic_domain passes."""
    assert fs.passes_domain_gate("Nature", ON_TOPIC) is True
    assert fs.passes_domain_gate("Nature", OFF_TOPIC) is False
    assert fs.passes_domain_gate("GRL", ON_TOPIC) is True
    assert fs.passes_domain_gate("Science", OFF_TOPIC) is False


def test_broad_venue_medical_excluded():
    assert (
        fs.passes_domain_gate(
            "Nature", "Breast cancer detection via ultrasound elastography."
        )
        is False
    )


def test_unknown_venue_is_treated_as_broad_gated():
    """An unknown venue (not in the registry) falls into the broad/gated pool:
    admitted only if it passes the domain filter."""
    assert fs.passes_domain_gate("Some Random Journal", ON_TOPIC) is True
    assert fs.passes_domain_gate("Some Random Journal", OFF_TOPIC) is False
    # ETH-RC ad-hoc label is unknown -> gated.
    assert fs.passes_domain_gate("ETH Zurich (Research Collection)", OFF_TOPIC) is False
    assert fs.passes_domain_gate("ETH Zurich (Research Collection)", ON_TOPIC) is True


def test_none_label_is_gated_like_broad():
    assert fs.passes_domain_gate(None, ON_TOPIC) is True
    assert fs.passes_domain_gate(None, OFF_TOPIC) is False
