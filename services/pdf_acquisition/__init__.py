"""Shared PDF validation and retrieval-status helpers.

The five-stage crawler that used to live in this package (institutional repository, discovery and OpenURL stubs,
plus a Playwright browser stage) is retired; its API routes answer HTTP 410. Full-text harvesting now lives in
``services/fullbundle_harvest.py`` (CLI: ``scripts/harvest_pdfs.py``). This package keeps the PDF validation
helpers and the ``RetrievalStatus`` contract that the importers and the harvester share.
"""

from .common import (
    AcquisitionConfig,
    AcquisitionContext,
    AcquisitionResult,
    AttemptRecord,
    ConfigError,
    PaperNotFoundError,
    PdfAcquisitionError,
    PdfValidationError,
    RetrievalStatus,
    StageResult,
    load_acquisition_config,
    validate_pdf_bytes,
)

__all__ = [
    "AcquisitionConfig",
    "AcquisitionContext",
    "AcquisitionResult",
    "AttemptRecord",
    "ConfigError",
    "PaperNotFoundError",
    "PdfAcquisitionError",
    "PdfValidationError",
    "RetrievalStatus",
    "StageResult",
    "load_acquisition_config",
    "validate_pdf_bytes",
]
