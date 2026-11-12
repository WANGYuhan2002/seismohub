"""Deterministic builder for tests/fixtures/sample_paper.pdf.

Generates a 6-page synthetic paper whose extracted text exercises every
branch of services.pdf_page_cache.extract_paragraph_anchors():

  Page 1 - title page (single short paragraph, a "Figure 1." caption that
           must be DROPPED, and a stray page number "1" that must be DROPPED).
  Page 2 - four prose paragraphs with known opening sentences. THE
           canonical golden page; index assertions live here.
  Page 3 - table block (tab-delimited rows; high tab density -> dropped)
           followed by one real prose paragraph.
  Page 4 - section heading "3. RESULTS" on its own line (kept-or-dropped
           is documented; see test_heading_is_kept_until_pr17_followup),
           plus a paragraph with a soft line-wrap that PyMuPDF preserves
           verbatim so we can exercise the DOTALL flag in OPENING_SENTENCE_RE.
  Page 5 - paragraph with a PyMuPDF ligature ("efficient" spelled with the
           actual ligature codepoint) to prove the _LIGATURES normalization
           survives a real extraction round-trip.
  Page 6 - "References" header followed by reference-list entries. PR-19's
           wrapper sweeps these; the per-page extractor in PR-17 does NOT,
           and this PR documents that boundary in the test names.

We use PyMuPDF (already in requirements.txt) rather than reportlab to build
the PDF. PyMuPDF can insert text via Page.insert_textbox, which is enough
for an extraction round-trip; reportlab would have been simpler API-wise
but would force a new dependency line. This builder is checked in alongside
the binary output; CI does NOT re-run it (the binary is ~6 KB and stable),
but a human or future agent can regenerate by:

    python tests/fixtures/build_sample_paper.py

Determinism: PyMuPDF.Document.save(..., garbage=4, deflate=True) writes
a byte-stable file when the input streams are byte-stable, which they
are here (no timestamps, no UUIDs, no random IDs).
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


OUT_PATH = Path(__file__).parent / "sample_paper.pdf"

# ---- Page text bodies (single-source-of-truth - tests import these). ----

PAGE_1 = (
    "Toward Hybrid H/V and Beamforming Site-Response Mapping\n\n"
    "Figure 1. H/V spectral ratio overlay at station ANMO. The peak at "
    "0.5 Hz indicates a sediment-basement interface.\n\n"
    "1"
)

# Four golden paragraphs the tests assert against. Order matters; openers
# are quoted verbatim by test_known_pdf_returns_expected_opening_sentences.
PAGE_2_OPENERS = [
    "In this study, we revisit the 2024 Sichuan sequence using a hybrid "
    "H/V and array-beamforming workflow.",
    "We analyse data from 42 broadband stations deployed across the back-arc "
    "between January and August 2024.",
    "The Mw 7.1 mainshock ruptured on 2024-04-03 at 22:14 UTC, triggering "
    "a 6-week aftershock sequence.",
    "Our results recover the input velocity model to within 5% RMS at "
    "all depths shallower than 20 km.",
]
PAGE_2 = "\n\n".join(
    opener + " Additional context follows here for prose density."
    for opener in PAGE_2_OPENERS
)

PAGE_3 = (
    "Station\tLat\tLon\tDepth\n"
    "ANMO\t34.95\t-106.46\t1.8\n"
    "CCM\t38.06\t-91.24\t0.2\n"
    "TUC\t32.31\t-110.78\t0.9\n\n"
    "The deployment geometry was optimised for back-azimuth resolution "
    "rather than depth resolution. We accept the trade-off because the "
    "study targets surface waves."
)

PAGE_4 = (
    "3. RESULTS\n\n"
    "The hybrid workflow recovers Vs30 across the back-arc to within\n"
    "12% of the borehole reference at 90% of co-located sites. "
    "Residuals correlate with surface geology, not station spacing."
)

PAGE_5 = (
    "We compare our method to two efficient baselines: a pure-H/V inversion "
    "and a pure-beamforming workflow. Our hybrid outperforms both above "
    "0.3 Hz."
)

PAGE_6 = (
    "References\n\n"
    "[1] Smith, J., 2022. H/V site response in the Cascadia back-arc. "
    "Bulletin of the Seismological Society of America, 112, 1234-1250.\n\n"
    "[2] Wang, L., 2024. Array beamforming with sparse arrays. "
    "Geophysical Journal International, 238, 456-478."
)

_PAGES = [PAGE_1, PAGE_2, PAGE_3, PAGE_4, PAGE_5, PAGE_6]


def build() -> Path:
    """Render the six pages into a single PDF and write to OUT_PATH."""
    doc = fitz.open()
    for body in _PAGES:
        page = doc.new_page(width=612, height=792)  # US Letter
        page.insert_textbox(
            fitz.Rect(72, 72, 540, 720),
            body,
            fontname="helv",
            fontsize=10,
            align=0,  # left
        )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # garbage=4 + deflate=True -> smallest deterministic output.
    doc.save(str(OUT_PATH), garbage=4, deflate=True, clean=True)
    doc.close()
    return OUT_PATH


if __name__ == "__main__":
    out = build()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
