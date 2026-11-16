"""Add transcribed sessions, immutable draft versions, and approved PDFs.

Revision ID: 0003_session_review
Revises: 0002_processing_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_session_review"
down_revision: str | None = "0002_processing_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("processing_jobs", sa.Column("audio_format", sa.String(length=8)))
    op.create_table(
        "clinical_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(length=36),
            sa.ForeignKey("processing_jobs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "professional_id",
            sa.String(length=36),
            sa.ForeignKey("professionals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            sa.String(length=36),
            sa.ForeignKey("patients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("template", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="TRANSCRIBED"),
        sa.Column("transcript_enc", sa.LargeBinary(), nullable=False),
        sa.Column("transcript_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_clinical_sessions_owner_patient_status",
        "clinical_sessions",
        ["professional_id", "patient_id", "status", "session_date"],
    )
    op.create_table(
        "session_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("clinical_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_enc", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fidelity_passed", sa.Boolean(), nullable=False),
        sa.Column("fidelity_issues_enc", sa.LargeBinary(), nullable=False),
        sa.Column("correction_reason_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "version", name="uq_session_versions_number"),
    )
    op.create_index(
        "ix_session_versions_session_created",
        "session_versions",
        ["session_id", "created_at"],
    )
    op.create_table(
        "clinical_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_version_id",
            sa.String(length=36),
            sa.ForeignKey("session_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_type",
            sa.String(length=16),
            nullable=False,
            server_default="evolution",
        ),
        sa.Column("content_enc", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("clinical_documents")
    op.drop_index("ix_session_versions_session_created", table_name="session_versions")
    op.drop_table("session_versions")
    op.drop_index("ix_clinical_sessions_owner_patient_status", table_name="clinical_sessions")
    op.drop_table("clinical_sessions")
    op.drop_column("processing_jobs", "audio_format")
