"""Database schema (SQLAlchemy Core, no ORM).

Portability rules, so that a later move to PostgreSQL is a connection-string change:
- timestamps (epoch milliseconds) and credit amounts are BigInteger;
- JSON columns have a JSONB variant on PostgreSQL;
- no STRICT tables, no JSON-path predicates, no dialect-specific column types elsewhere.

Bump SCHEMA_VERSION and append a migration in `store.py` for every change to this file.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_VERSION = 1

JOB_STATUSES = ("queued", "submitting", "running", "completed", "failed", "canceled", "moderated")
ACTIVE_JOB_STATUSES = ("queued", "submitting", "running")
TERMINAL_JOB_STATUSES = ("completed", "failed", "canceled", "moderated")
MEDIA_STATUSES = ("pending", "ready", "failed", "deleted")
LEDGER_KINDS = ("grant", "spend", "refund", "adjust")

metadata = MetaData()


def _json() -> JSON:
    return JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


ID = String(40)
_PK_INT = BigInteger().with_variant(Integer, "sqlite")  # SQLite autoincrement needs INTEGER PRIMARY KEY

meta = Table(
    "meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

accounts = Table(
    "accounts",
    metadata,
    Column("workspace_id", ID, primary_key=True),
    Column("balance", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    CheckConstraint("balance >= 0", name="accounts_balance_non_negative"),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", _PK_INT, primary_key=True, autoincrement=True),
    Column("workspace_id", ID, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("balance_after", BigInteger, nullable=False),
    Column("ref_type", String(16)),
    Column("ref_id", ID),
    Column("day", String(10), nullable=False),
    Column("note", Text),
    Column("created_at", BigInteger, nullable=False),
    CheckConstraint(_in("kind", LEDGER_KINDS), name="ledger_kind"),
    CheckConstraint("amount >= 0", name="ledger_amount_non_negative"),
    # One spend and at most one refund per reference. Rows without a reference (grants)
    # have NULL ref_id and are never considered duplicates.
    UniqueConstraint("ref_type", "ref_id", "kind", name="ledger_once_per_ref"),
    Index("ledger_by_workspace", "workspace_id", "id"),
)

daily_spend = Table(
    "daily_spend",
    metadata,
    Column("workspace_id", ID, primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("spent", BigInteger, nullable=False, default=0),
    CheckConstraint("spent >= 0", name="daily_spend_non_negative"),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("workspace_id", ID, primary_key=True),
    Column("tool", String(48), primary_key=True),
    Column("key", String(128), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response_json", Text),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Index("idempotency_expiry", "expires_at"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", ID, primary_key=True),
    Column("workspace_id", ID, nullable=False),
    Column("group_id", ID, nullable=False),
    Column("group_index", Integer, nullable=False, default=0),
    Column("tool", String(48), nullable=False),
    Column("output_type", String(16), nullable=False),
    Column("model_id", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("params_json", _json(), nullable=False),
    Column("adjustments_json", _json()),
    Column("remote_id", String(255)),
    Column("remote_state_json", _json()),
    Column("lease_owner", String(160)),
    Column("lease_expires_at", BigInteger),
    Column("next_poll_at", BigInteger, nullable=False),
    Column("deadline_at", BigInteger, nullable=False),
    Column("cancel_requested_at", BigInteger),
    Column("progress", Float),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column("credits", BigInteger, nullable=False, default=0),
    Column("folder_id", ID),
    Column("client_context", Text),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("started_at", BigInteger),
    Column("finished_at", BigInteger),
    CheckConstraint(_in("status", JOB_STATUSES), name="jobs_status"),
    Index("jobs_by_workspace", "workspace_id", "id"),
    Index("jobs_by_group", "group_id", "group_index"),
    Index(
        "jobs_due",
        "next_poll_at",
        sqlite_where=text(_in("status", ACTIVE_JOB_STATUSES)),
        postgresql_where=text(_in("status", ACTIVE_JOB_STATUSES)),
    ),
)

media = Table(
    "media",
    metadata,
    Column("id", ID, primary_key=True),
    Column("workspace_id", ID, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("mime", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("job_id", ID),
    Column("output_index", Integer),
    Column("blob_key", String(255)),
    Column("size_bytes", BigInteger),
    Column("sha256", String(64)),
    Column("width", Integer),
    Column("height", Integer),
    Column("duration_ms", BigInteger),
    Column("filename", String(255)),
    Column("origin", Text),
    Column("upload_nonce", String(64)),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger),
    CheckConstraint(_in("status", MEDIA_STATUSES), name="media_status"),
    UniqueConstraint("job_id", "output_index", name="media_once_per_output"),
    Index("media_by_workspace", "workspace_id", "id"),
    Index("media_by_sha256", "sha256"),
)

uploads = Table(
    "uploads",
    metadata,
    Column("id", ID, primary_key=True),
    Column("workspace_id", ID, nullable=False),
    Column("media_id", ID, ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
    Column("max_bytes", BigInteger, nullable=False),
    Column("received_bytes", BigInteger, nullable=False, default=0),
    Column("next_index", Integer, nullable=False, default=0),
    Column("last_chunk_sha256", String(64)),
    Column("status", String(16), nullable=False, default="open"),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
)

projects = Table(
    "projects",
    metadata,
    Column("id", ID, primary_key=True),
    Column("workspace_id", ID, nullable=False),
    Column("name", String(100), nullable=False),
    Column("description", Text),
    Column("root_folder_id", ID),
    Column("archived_at", BigInteger),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Index("projects_by_workspace", "workspace_id", "id"),
)

folders = Table(
    "folders",
    metadata,
    Column("id", ID, primary_key=True),
    Column("project_id", ID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("parent_id", ID, ForeignKey("folders.id", ondelete="CASCADE")),
    Column("name", String(100), nullable=False),
    Column("depth", Integer, nullable=False, default=0),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("parent_id", "name", name="folders_unique_name_per_parent"),
    Index("folders_by_project", "project_id", "parent_id"),
)

placements = Table(
    "placements",
    metadata,
    Column("id", ID, primary_key=True),
    Column("folder_id", ID, ForeignKey("folders.id", ondelete="CASCADE"), nullable=False),
    Column("project_id", ID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("source_type", String(8), nullable=False),
    Column("source_id", ID, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    CheckConstraint("source_type IN ('job', 'media')", name="placements_source_type"),
    # Attaching the same source to the same folder twice reuses the placement.
    UniqueConstraint("folder_id", "source_type", "source_id", name="placements_once_per_folder"),
    Index("placements_by_source", "source_type", "source_id"),
)

preferences = Table(
    "preferences",
    metadata,
    Column("workspace_id", ID, primary_key=True),
    Column("key", String(64), primary_key=True),
    Column("value_json", _json()),
)

app_runs = Table(
    "app_runs",
    metadata,
    Column("id", ID, primary_key=True),
    Column("workspace_id", ID, nullable=False),
    Column("app_id", String(64), nullable=False),
    Column("action", String(64), nullable=False),
    Column("manifest_revision", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("input_json", _json()),
    Column("output_json", _json()),
    Column("error_json", _json()),
    Column("credits", BigInteger, nullable=False, default=0),
    Column("group_id", ID),
    Column("created_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger),
    Index("app_runs_by_workspace", "workspace_id", "id"),
)
