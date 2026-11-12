from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import backref, declarative_base, relationship


Base = declarative_base()


class Paper(Base):
    __tablename__ = "papers"

    id = Column(String(64), primary_key=True, index=True)
    source = Column(String(64), nullable=False, default="unknown")
    title = Column(Text, nullable=False)
    journal = Column(String(255), nullable=False, default="Others")
    topics = Column(JSON, nullable=False, default=list)
    institutions = Column(JSON, nullable=False, default=list)
    keywords = Column(JSON, nullable=False, default=list)  # v2: OpenAlex concepts at fetch-time
    authors = Column(JSON, nullable=False, default=list)
    published_date = Column(String(32), nullable=True)
    datetime_utc = Column(String(64), nullable=True)
    link = Column(Text, nullable=True)
    doi = Column(String(255), nullable=True, index=True)
    abstract = Column(Text, nullable=True)
    abstract_flag = Column(String(32), nullable=False, default="missing")
    pdf_path = Column(Text, nullable=True)
    # SHA-256 of the stored PDF bytes — used for upload dedup and to detect
    # external annotation changes (mtime changes when annotations are written
    # back by Skim / PDF Expert / Preview). Populated by services/pdf_naming
    # write_pdf_atomic(). Indexed UNIQUE via ad-hoc migration in db.py because
    # SQLite ALTER TABLE cannot add column-level UNIQUE constraints.
    pdf_sha256 = Column(String(64), nullable=True, index=True)
    # Wall-clock when the user last opened this paper in an external reader
    # (POST /api/papers/{id}/open). Drives the "Read 3 days ago" badge.
    last_opened_at = Column(DateTime, nullable=True)
    # Roadmap V2 item 41 — Why Strip externalized intent (T17 ADHD-aware).
    reading_intent = Column(Text, nullable=True)
    reading_intent_at = Column(DateTime, nullable=True)
    # Roadmap V2 item 1 — Forced two-sentence takeaway (T1 reading-exit).
    user_takeaway_what = Column(Text, nullable=True)
    user_takeaway_why = Column(Text, nullable=True)
    user_takeaway_at = Column(DateTime, nullable=True)
    # Roadmap V2 item 16-prime — Save-for-follow-up personal queue.
    follow_up = Column(Boolean, nullable=False, default=False)
    follow_up_at = Column(DateTime, nullable=True)
    # 2026-05-19 — Structured 7-question reading notes (七问读书法).
    # JSON dict stored as TEXT: {q1..q7, personal} — all optional free text.
    reading_notes = Column(Text, nullable=True)
    reading_notes_at = Column(DateTime, nullable=True)

    # BRIEF v2 §3 — Cheap semantic-match embedding of title+abstract (NO PDF,
    # NO full Gemini Pro analysis). Computed at ingest by the ingest-pipeline
    # package via services/embedding_service.py (text_for_embedding -> embed_text
    # -> encode_vector). The semantic tracker predicate scores papers by
    # cosine(query_embedding, embedding) >= threshold (numpy brute force — NO
    # sqlite-vec). All three columns are nullable so a paper persists fine even
    # when embedding is skipped (missing API key / quota / network failure —
    # embedding_service swallows the error to None); such a paper simply never
    # matches a semantic predicate until a re-embed/backfill pass recovers it.
    #
    # `embedding` holds the JSON-encoded float list (services.embedding_service.
    # encode_vector output) in a TEXT cell — JSON not BLOB so the cell stays
    # human-inspectable / sqlite-greppable. `embedding_model` records the
    # (model, dim) stability contract (services.embedding_service.EMBED_MODEL,
    # e.g. "gemini-embedding-001"): stored vectors are only comparable within
    # one model id, so consumers persist it here and skip cosine when it differs.
    # `embedded_at` is the wall-clock when the vector was last (re)computed.
    embedding = Column(Text, nullable=True)
    embedding_model = Column(String(64), nullable=True)
    embedded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # PDF acquisition pipeline state (populated by services/pdf_acquisition).
    # All columns are nullable / defaulted so existing rows migrate cleanly.
    retrieval_status = Column(String(32), nullable=False, default="not_attempted")
    retrieval_source = Column(String(64), nullable=True)
    retrieval_source_url = Column(Text, nullable=True)
    retrieval_last_stage = Column(String(16), nullable=True)
    retrieval_error_reason = Column(Text, nullable=True)
    retrieval_attempts_count = Column(Integer, nullable=False, default=0)
    retrieval_attempted_at = Column(DateTime, nullable=True)
    retrieval_succeeded_at = Column(DateTime, nullable=True)
    retrieval_next_retry_at = Column(DateTime, nullable=True)

    ai_analysis = relationship(
        "AIAnalysis",
        back_populates="paper",
        uselist=False,
        cascade="all, delete-orphan",
    )
    retrieval_attempts = relationship(
        "RetrievalAttempt",
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="RetrievalAttempt.started_at.desc()",
    )


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    paper_id = Column(String(64), ForeignKey("papers.id"), nullable=False, unique=True, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    summary = Column(Text, nullable=True)
    one_sentence_takeaway = Column(Text, nullable=True)
    relevance_score = Column(Integer, nullable=False, default=0)
    recommended_tags = Column(JSON, nullable=False, default=list)
    topic_matches = Column(JSON, nullable=False, default=list)
    # v3 faceted labels: {primary_topic[], method[], data_type[], target[], paper_role}
    facets = Column(JSON, nullable=True)
    used_sources = Column(JSON, nullable=False, default=list)
    pdf_used = Column(Boolean, nullable=False, default=False)
    evidence_based_on = Column(String(64), nullable=True)
    evidence_notes = Column(Text, nullable=True)
    keywords = Column(JSON, nullable=False, default=list)
    # BRIEF item C — model identity for the AI Analysis card pill.
    # `analysis_model` is the wire-level model ID that ACTUALLY produced
    # the analysis payload (could be the fallback on primary failure —
    # see services/llm_client.py:_last_model_used). `analysis_model_label`
    # is the human-readable freeze ("Gemini 3.1 Pro") captured at write
    # time so retired wire IDs still render a stable badge months later.
    # Both are nullable — every pre-PR-14 row stays valid; the renderer
    # falls back to "Unknown" when NULL (see qa-model-badge.md §6).
    analysis_model = Column(String(64), nullable=True)
    analysis_model_label = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    paper = relationship("Paper", back_populates="ai_analysis")


class AppState(Base):
    __tablename__ = "app_state"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# M2: PDF page cache  (G6)
# ---------------------------------------------------------------------------

class PaperPageCache(Base):
    """Per-paper page-text cache extracted by PyMuPDF.

    Populated on first PDF analysis or QA chat.  Powers page-anchor chips in
    the structured AI card (G4) and the PDF QA sidebar (G5).

    ``pages`` schema:
      [{"page": 1, "text": "Introduction ...", "chars": 3218}, ...]
    """

    __tablename__ = "paper_page_caches"

    paper_id = Column(String(64), ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    pages = Column(JSON, nullable=False, default=list)
    page_count = Column(Integer, nullable=False, default=0)
    extracted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    paper = relationship("Paper", backref="page_cache", uselist=False)


# ---------------------------------------------------------------------------
# M2: Multi-project / Workspaces  (must-do #4)
# ---------------------------------------------------------------------------

def _new_uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    """Top-level workspace container.

    The canonical taxonomy is the (kind, scope, cadence) triple (see
    changelog/06-master-plan/PLAN.md §1.1 and changelog/04-feature-design/
    03-type-split-migration.md §2). The legacy `type` column is preserved
    as a write-through shadow for one release per PLAN §9 ("Shadow-column
    policy") and surfaced via the read-only `legacy_type` hybrid below.

    Mapping (see also db.LEGACY_PROJECT_TYPE_MAP):
      kind="tracker"                                 ← "topic_monitor"
      kind="collection", default_sort_by="custom"    ← "static"
      kind="tracker", scope="personal",                 ← "monthly"
        cadence="monthly", default_sort_by="published_date"
    """

    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=_new_uuid)
    name = Column(String(255), nullable=False)
    # LEGACY — populated for backwards compat (PR-01 migration leaves it
    # in place); ORM consumers must read `legacy_type` (hybrid) instead
    # of `type` going forward. Dropped in a follow-up release once every
    # `main.py` call site has been flipped (see PLAN §9 R-shadow).
    type = Column(String(32), nullable=False, default="monthly")
    # New triple (PR-01 backfilled from `type`). All three may be NULL
    # on rows the backfill has not yet seen; consumers must tolerate
    # NULL during the migration window (see legacy_type fallback).
    kind = Column(String(16), nullable=True)            # "tracker" | "collection"
    scope = Column(String(16), nullable=True)           # "group" | "personal" — tracker only
    cadence = Column(String(16), nullable=True)         # "daily" | "weekly" | "monthly" — tracker only
    default_sort_by = Column(String(32), nullable=True) # see PLAN §1.1 (9 keys); NULL means use frontend default
    # SQLite ALTER TABLE cannot add a NOT NULL column without a default that
    # matches every existing row; PR-01's ALTER added it nullable. We mirror
    # nullable=True in the ORM so create_all on a fresh DB also makes the
    # column nullable — the raw-SQL backfill in db._backfill_default_project
    # never sets this field, and a NOT NULL would crash boot. Application
    # code that reads this column should coalesce to "compact" on NULL.
    table_mode = Column(String(16), nullable=True, default="compact")  # "compact" | "expanded" (BRIEF item F)
    description = Column(Text, nullable=True)
    color_tag = Column(String(16), nullable=True)
    icon = Column(String(16), nullable=True)  # custom emoji prefix shown before the project name
    # For topic_monitor: {"topics": [...]}
    # For static: {"import_source": "filename.bib"}
    # For monthly: {} or {"auto_folder": true}
    config = Column(JSON, nullable=False, default=dict)
    sort_order = Column(Integer, nullable=False, default=0)  # for sidebar ordering
    # FN-B1-A04-1 / FN-B1-A08-1 — Hard-flag the seeded "Monthly Literature
    # Review" project so it can never be deleted via API regardless of how
    # many other projects exist. Set to 1 exactly once in
    # db._backfill_default_project; rejected by PATCH /api/projects/{id}.
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)
    # Tracker telemetry (PR-01 migration: see db._PROJECT_COLUMNS). The DDL
    # lands at startup via idempotent ALTER TABLE; these ORM columns let
    # main.py read/write them without falling back to getattr(). Both
    # datetimes are nullable so a freshly seeded tracker (last_match_at IS
    # NULL, last_viewed_at IS NULL) correctly surfaces "everything is
    # unread" on first open per 01-tracker-data-model.md §4.
    last_match_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    # Denormalized counter — currently computed on the fly by _project_to_dict
    # via COUNT(project_papers WHERE added_at > last_viewed_at). The stored
    # column is reserved for a future cache (PLAN §1.6 footnote). Default 0
    # so the column matches the migration's NOT NULL DEFAULT 0 contract.
    # server_default="0" ensures create_all on a fresh DB emits the same
    # DEFAULT 0 clause as the ALTER-based migration on a legacy DB; without
    # it raw INSERTs that omit the column trip the NOT NULL constraint.
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")

    papers = relationship(
        "ProjectPaper",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectPaper.added_at.desc()",
    )
    imports = relationship(
        "ProjectImport",
        back_populates="project",
        cascade="all, delete-orphan",
    )

    # ------------------------------------------------------------------
    # Read-side compat shim: legacy_type
    # ------------------------------------------------------------------
    # Inverse of db.LEGACY_PROJECT_TYPE_MAP. Returns "monthly" |
    # "topic_monitor" | "static" derived from the new triple so the 14
    # hard-coded `project.type == "monthly"` style branches in main.py
    # keep evaluating correctly while PR-04+ flip them site-by-site.
    #
    # MUST be a SQLAlchemy hybrid_property (not a plain @property):
    # `db.py:205` and the import guard at `main.py:2043` use it inside
    # a `.filter()` clause and a plain property silently breaks SQL
    # filters. See 04-feature-design/03-type-split-migration.md §2.
    #
    # No `@legacy_type.expression` is added in this round — every
    # current SQL caller can use the equivalent native triple
    # (`Project.kind == "tracker"` etc.) directly; the expression-side
    # mapping is a cheap follow-up if a caller needs it.
    @hybrid_property
    def legacy_type(self) -> str:
        # 2026-05-25 reclassification: the seeded "Monthly Literature
        # Review" is now kind='tracker' (auto-bucketed by YYYY-MM) but
        # still surfaces as legacy_type='monthly' so call sites that
        # special-case the default via `project.type == 'monthly'`
        # (the auto-add path in main.py, several tests) keep working.
        if self.kind == "tracker":
            if getattr(self, "is_default", False):
                return "monthly"
            return "topic_monitor"
        if self.kind == "collection":
            return "static" if self.default_sort_by == "custom" else "monthly"
        # Defence in depth: row not yet seen by the PR-01 backfill, OR
        # PR-01 was reverted. Fall back to the stored legacy column,
        # which the shadow-column policy guarantees stays populated.
        return (self.type or "monthly")


class ProjectPaper(Base):
    """Association between a project and a paper, with folder grouping.

    folder examples:
      monthly project       → "2024-11"
      topic_monitor project → "Full-waveform inversion"
      static project        → "imported"
    source values: "backfill" | "upload" | "watch" | "import" | "manual"
    """

    __tablename__ = "project_papers"
    __table_args__ = (
        UniqueConstraint("project_id", "paper_id", name="uq_project_paper"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id = Column(String(64), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    # Sub-folder within the project (e.g. "2024-11" for monthly, topic name for topic_monitor)
    folder = Column(String(255), nullable=False, default="")
    # How this paper was added to the project
    source = Column(String(32), nullable=False, default="manual")
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # In-folder ordering for collections with default_sort_by="custom".
    # NOT NULL with default 0 so legacy rows behave like "no opinion" —
    # ties broken by added_at DESC. Active only when the parent project's
    # default_sort_by="custom" (see 04-feature-design/02-collection-data-
    # model.md §2 row "position"). Frontend writes midpoint-of-INT on
    # drag-drop; re-pack to dense integers on collision.
    position = Column(Integer, nullable=False, default=0)
    # "When did this paper first appear in this project's view?" — drives
    # the tracker unread badge (PLAN §1.6). For backfill-source rows this
    # equals added_at (the PR-01 migration UPDATE sets it). For trackers,
    # the auto-assign re-match path stamps it fresh so a re-matched paper
    # surfaces as unread again. Nullable to let SQLite ALTER TABLE add
    # the column without a server_default; PR-01's UPDATE backfills
    # historical rows from added_at and the insert path in main.py /
    # analyze_service sets it on every new row. See 04-feature-design/
    # 01-tracker-data-model.md §4.
    seen_in_project_at = Column(DateTime, nullable=True)
    # BRIEF v2 §1 — Best predicate score for this paper within this tracker
    # (0..1), written by the predicate-based matcher (analyze_service
    # _auto_assign_to_topic_monitors delegating to the predicate registry).
    # Conventionally the best semantic cosine, but any predicate type may set
    # it. Tracker views sort by this DESC (BRIEF v2 §6). Nullable so legacy
    # rows and collection memberships (no tracker scoring) stay valid and the
    # SQLite ALTER TABLE can add the column without a server_default; the
    # renderer / sort coalesces NULL to the bottom.
    match_score = Column(Float, nullable=True)

    project = relationship("Project", back_populates="papers")
    paper = relationship("Paper", backref="project_memberships")


class ProjectImport(Base):
    """Tracks BibTeX/RIS import operations for static projects (dedup guard).

    source_hash is a SHA-256 of the import file bytes so re-importing the
    same file is silently skipped.
    """

    __tablename__ = "project_imports"
    __table_args__ = (
        UniqueConstraint("project_id", "source_hash", name="uq_project_import_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # Scoped UNIQUE: same .bib file may be imported into multiple projects.
    # See FN-B3-A04-2 + FN-B3-A13-1.
    source_hash = Column(String(64), nullable=False)
    source_filename = Column(String(255), nullable=True)
    imported_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    paper_count = Column(Integer, nullable=False, default=0)

    project = relationship("Project", back_populates="imports")


# ---------------------------------------------------------------------------

class RetrievalAttempt(Base):
    """One row per PDF acquisition attempt against a single source URL.

    The pipeline writes one row as each candidate URL is tried, which gives
    a complete audit trail of what was attempted for every paper. This is
    the "可追踪来源" requirement from the design — every successful PDF has
    a provable chain of prior attempts and every failure has a recorded
    reason.
    """

    __tablename__ = "retrieval_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    paper_id = Column(String(64), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(String(16), nullable=False)  # "stage1".."stage5"
    source = Column(String(64), nullable=True)  # "openalex", "unpaywall", "crossref_tdm", ...
    url_attempted = Column(Text, nullable=True)
    outcome = Column(String(32), nullable=False)  # RetrievalStatus enum value
    http_status = Column(Integer, nullable=True)
    bytes_downloaded = Column(Integer, nullable=True)
    error_reason = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    paper = relationship("Paper", back_populates="retrieval_attempts")


# ---------------------------------------------------------------------------
# Week-1 / M-01: QA chat persistence (BRIEF item D)
#
# Row-per-turn storage of the per-paper AI Q&A conversation. Supersedes the
# papers.chat_history JSON blob that 01-current-state/04-schema.md §1 had
# pencilled in — vote Q8 (49/50) + vote D3 (50/50) ratified the table shape
# over the blob, and the UNIQUE(paper_id, position) constraint below was
# requested verbatim by typezealot-38 to make position-allocation drift loud.
#
# Columns are the scope-locked subset of 04-feature-design/12 §2:
#   * model_id    = stable LLM identifier ("gemini-3.1-pro-preview") — what
#                   we store for audit / replay / item C badge logic
#   * model_label = human label ("Gemini 3.1 Pro") — denormalised so the
#                   QA modal renders the badge with one row read, no JOIN
#                   against an LLM-registry table that does not exist yet
#   * citations   = JSON list of citation envelopes (shape defined in
#                   04-feature-design/15 §2.1) — empty list for user / system
#                   rows, populated for assistant rows once PR-07 lands
# Both model_* columns are nullable: user/system rows leave them NULL, and
# legacy assistant rows imported before PR-07 ships also stay NULL until
# back-filled.
# ---------------------------------------------------------------------------

class QAMessage(Base):
    __tablename__ = "qa_messages"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    paper_id    = Column(String(64), ForeignKey("papers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    role        = Column(String(16), nullable=False)         # "user"|"assistant"|"system"
    text        = Column(Text, nullable=False)
    model_id    = Column(String(64), nullable=True)          # e.g. "gemini-3.1-pro-preview"
    model_label = Column(String(64), nullable=True)          # e.g. "Gemini 3.1 Pro"
    citations   = Column(JSON, nullable=False, default=list) # list of envelope dicts
    position    = Column(Integer, nullable=False)            # monotonic per paper_id
    archived    = Column(Boolean, nullable=False, default=False)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("paper_id", "position", name="uq_qa_paper_position"),
        Index("ix_qa_paper_archived_pos", "paper_id", "archived", "position"),
    )

    paper = relationship(
        "Paper",
        backref=backref("qa_messages",
                        cascade="all, delete-orphan",
                        order_by="QAMessage.position"),
    )


# ---------------------------------------------------------------------------
# Wave-9 — UserPaperState (T8 era; surfaced via PR-93 UI)
#
# Stores per-paper read/archive/dismiss state so the user can curate the
# paper list without deleting rows. Each flag is a nullable DateTime — a
# non-NULL value means the action is "on" (and records WHEN it happened);
# NULL means "off". Single-row-per-paper table (PK is paper_id), so the
# /api/papers/{id}/{read,archive,dismiss} toggles upsert.
#
# Read = "I've seen this card / I'm done with it"   — list-level mute
# Archive = "I want this out of the active feed"   — Inbox dim
# Dismiss = "Hide this paper everywhere"          — Papers grid filter
#
# All three are user-driven; the tracker assignment and follow-up flag
# stay in their own columns on `papers` (different lifecycle owner).
# ---------------------------------------------------------------------------

class UserPaperState(Base):
    __tablename__ = "user_paper_states"

    paper_id     = Column(String(64), ForeignKey("papers.id", ondelete="CASCADE"),
                          primary_key=True, index=True)
    read_at      = Column(DateTime, nullable=True)
    archived_at  = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    # 2026-06-16 — ❤️ "interested" signal: a persistent taste marker (WHAT the
    # user cares about), distinct from ★ follow_up (the transient to-read
    # queue). Feeds the relevance interest profile as a positive example.
    liked_at     = Column(DateTime, nullable=True)
    updated_at   = Column(DateTime, nullable=False, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

    paper = relationship(
        "Paper",
        backref=backref("user_state", uselist=False, cascade="all, delete-orphan"),
    )
