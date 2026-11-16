"""SQLAlchemy models for professional identity and patient administration."""

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Professional(Base):
    __tablename__ = "professionals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_id_digest: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    name_enc: Mapped[str] = mapped_column(Text)
    profession_enc: Mapped[str] = mapped_column(Text)
    council_registry_enc: Mapped[str] = mapped_column(Text)
    specialty_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Fortaleza")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    professional_id: Mapped[str] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
    )
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (Index("ix_patients_professional_status", "professional_id", "active"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    professional_id: Mapped[str] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name_enc: Mapped[str] = mapped_column(Text)
    alias_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    birth_date_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    template: Mapped[str] = mapped_column(String(32), default="SOAP")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Guardian(Base):
    __tablename__ = "guardians"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    name_enc: Mapped[str] = mapped_column(Text)
    relationship_enc: Mapped[str] = mapped_column(Text)
    contact_enc: Mapped[str | None] = mapped_column(Text, nullable=True)


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (Index("ix_consents_patient_granted", "patient_id", "granted_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    terms_version: Mapped[str] = mapped_column(String(64))
    purpose_enc: Mapped[str] = mapped_column(Text)
    evidence_ref_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    description_enc: Mapped[str] = mapped_column(Text)
    area_enc: Mapped[str] = mapped_column(Text)
    indicator_type: Mapped[str] = mapped_column(String(32))
    mastery_criterion_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index("ix_processing_jobs_ready", "status", "available_at", "created_at"),
        Index("ix_processing_jobs_owner", "professional_id", "patient_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    professional_id: Mapped[str] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    audio_format: Mapped[str | None] = mapped_column(String(8), nullable=True)
    payload_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ClinicalSession(Base):
    __tablename__ = "clinical_sessions"
    __table_args__ = (
        Index(
            "ix_clinical_sessions_owner_patient_status",
            "professional_id",
            "patient_id",
            "status",
            "session_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    professional_id: Mapped[str] = mapped_column(
        ForeignKey("professionals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    template: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="TRANSCRIBED", nullable=False)
    transcript_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    transcript_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SessionVersion(Base):
    __tablename__ = "session_versions"
    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_session_versions_number"),
        Index("ix_session_versions_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("clinical_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fidelity_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fidelity_issues_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    correction_reason_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClinicalDocument(Base):
    __tablename__ = "clinical_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_version_id: Mapped[str] = mapped_column(
        ForeignKey("session_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_type: Mapped[str] = mapped_column(String(16), nullable=False, default="evolution")
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
