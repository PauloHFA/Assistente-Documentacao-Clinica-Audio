"""Transcribed sessions, immutable review versions, approvals, and authorized history."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from carevoice.db.models import (
    AuditLog,
    ClinicalDocument,
    ClinicalSession,
    Consent,
    Goal,
    Patient,
    ProcessingJob,
    Professional,
    SessionVersion,
    new_id,
    utc_now,
)
from carevoice.documents.pdf import PDFIdentity, PDFRenderer
from carevoice.domain.identity import (
    AuthenticationError,
    AuthorizationError,
    ConsentRequired,
    ResourceNotFound,
)
from carevoice.fidelity.verifier import FidelityReport, verify_draft_fidelity
from carevoice.security.crypto import FieldCipher
from carevoice.structure.schemas import (
    ABCContent,
    FreeContent,
    SOAPContent,
    StructuredDraft,
)

SessionStatus = Literal["TRANSCRIBED", "DRAFT", "APPROVED", "ARCHIVED", "DISCARDED"]


class DraftRejected(Exception):
    """Raised when a draft cannot be approved or does not match its source session."""


class TranscriptExpired(Exception):
    """Raised when a transcript is no longer available under its retention deadline."""


class IntegrityViolation(Exception):
    """Raised when the stored content hash does not match its immutable version."""


@dataclass(frozen=True)
class TranscribedSession:
    id: str
    patient_id: str
    session_date: date
    template: str
    status: SessionStatus


@dataclass(frozen=True)
class SavedDraft:
    session_id: str
    version_id: str
    version: int
    fidelity: FidelityReport


@dataclass(frozen=True)
class ApprovalResult:
    session_id: str
    version_id: str
    document_id: str
    pdf: bytes


@dataclass(frozen=True)
class SessionSummary:
    id: str
    patient_id: str
    session_date: date
    template: str
    version: int
    title: str


@dataclass(frozen=True)
class SearchMatch:
    session_id: str
    patient_id: str
    session_date: date
    status: SessionStatus
    snippet: str


@dataclass(frozen=True)
class GoalMeasurePoint:
    session_id: str
    session_date: date
    value: Decimal
    unit: str
    is_approximate: bool
    original_expression: str


@dataclass(frozen=True)
class GoalProgressSeries:
    goal_id: str
    description: str
    indicator_type: str
    mastery_criterion: str
    points: tuple[GoalMeasurePoint, ...]
    trend_summary: str


@dataclass(frozen=True)
class AuditEventRecord:
    id: str
    action: str
    object_type: str
    object_id: str
    created_at: datetime


class ClinicalRecordService:
    """Owns the approval gate; only approved versions can produce final PDFs."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        cipher: FieldCipher,
        pdf_renderer: PDFRenderer | None = None,
        *,
        transcript_retention_days: int = 30,
        minimum_retention_days: int = 0,
    ) -> None:
        if transcript_retention_days < 0:
            raise ValueError("Transcript retention cannot be negative")
        if minimum_retention_days < 0:
            raise ValueError("Minimum retention cannot be negative")
        self._sessions = sessions
        self._cipher = cipher
        self._pdf_renderer = pdf_renderer or PDFRenderer()
        self._transcript_retention_days = transcript_retention_days
        self._minimum_retention_days = minimum_retention_days

    def create_transcribed_session(
        self,
        *,
        job_id: str,
        professional_id: str,
        patient_id: str,
        transcript: str,
        session_date: date | None = None,
    ) -> TranscribedSession:
        normalized_transcript = transcript.strip()
        if not normalized_transcript:
            raise ValueError("Transcription cannot be empty")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ClinicalSession).where(ClinicalSession.job_id == job_id)
            )
            if existing is not None:
                if existing.professional_id != professional_id or existing.patient_id != patient_id:
                    raise AuthorizationError("Processing job is linked to another session owner")
                return _session_record(existing)

            job = session.get(ProcessingJob, job_id)
            professional = session.get(Professional, professional_id)
            patient = session.get(Patient, patient_id)
            consent_id = session.scalar(
                select(Consent.id).where(
                    Consent.patient_id == patient_id,
                    Consent.revoked_at.is_(None),
                )
            )
            if (
                job is None
                or job.professional_id != professional_id
                or job.patient_id != patient_id
            ):
                raise ResourceNotFound("Processing job not found")
            if professional is None or not professional.active:
                raise AuthenticationError("Professional access is not active")
            if patient is None or not patient.active or patient.professional_id != professional_id:
                raise ResourceNotFound("Patient not found")
            if consent_id is None:
                raise ConsentRequired("Current consent is required before saving transcription")
            if job.status != "running":
                raise DraftRejected("Processing job is not running")
            resolved_session_date = (
                session_date or datetime.now(ZoneInfo(professional.timezone)).date()
            )

            record = ClinicalSession(
                id=new_id(),
                job_id=job_id,
                professional_id=professional_id,
                patient_id=patient_id,
                session_date=resolved_session_date,
                template=patient.template,
                status="TRANSCRIBED",
                transcript_enc=self._cipher.encrypt_bytes(normalized_transcript.encode("utf-8")),
                transcript_expires_at=utc_now() + timedelta(days=self._transcript_retention_days),
            )
            session.add(record)
            session.flush()
            _audit(session, professional_id, "session.transcribed", "clinical_session", record.id)
            return _session_record(record)

    def discard_transcription(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            clinical_session = session.scalar(
                select(ClinicalSession).where(ClinicalSession.job_id == job_id).with_for_update()
            )
            if clinical_session is None:
                return
            clinical_session.transcript_enc = b""
            clinical_session.transcript_expires_at = utc_now()
            clinical_session.status = "DISCARDED"
            _audit(
                session,
                clinical_session.professional_id,
                "session.transcription_discarded",
                "clinical_session",
                clinical_session.id,
            )

    def purge_expired_transcripts(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("Transcript cleanup limit must be between 1 and 1000")
        purged = 0
        with self._sessions.begin() as session:
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.transcript_expires_at <= utc_now(),
                    ClinicalSession.transcript_enc != b"",
                )
                .order_by(ClinicalSession.transcript_expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for record in records:
                record.transcript_enc = b""
                if record.status == "TRANSCRIBED":
                    record.status = "DISCARDED"
                _audit(
                    session,
                    record.professional_id,
                    "transcript.expired",
                    "clinical_session",
                    record.id,
                )
                purged += 1
        return purged

    def get_transcript(self, chat_id: str, session_id: str) -> str:
        transcript: str | None = None
        expired = False
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            clinical_session = _owned_session(session, professional.id, session_id)
            if _as_utc(clinical_session.transcript_expires_at) <= utc_now():
                clinical_session.transcript_enc = b""
                expired = True
                _audit(
                    session,
                    professional.id,
                    "transcript.expired",
                    "clinical_session",
                    session_id,
                )
            else:
                transcript = self._cipher.decrypt_bytes(clinical_session.transcript_enc).decode(
                    "utf-8"
                )
                _audit(
                    session,
                    professional.id,
                    "transcript.read",
                    "clinical_session",
                    session_id,
                )
        if expired:
            raise TranscriptExpired("Transcript retention period has expired")
        if transcript is None:
            raise TranscriptExpired("Transcript is unavailable")
        return transcript

    def save_draft(
        self,
        chat_id: str,
        session_id: str,
        draft_payload: str | bytes,
        *,
        correction_reason: str | None = None,
    ) -> SavedDraft:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            professional_id = professional.id
        return self.save_draft_for_professional(
            professional_id,
            session_id,
            draft_payload,
            correction_reason=correction_reason,
        )

    def save_draft_for_professional(
        self,
        professional_id: str,
        session_id: str,
        draft_payload: str | bytes,
        *,
        correction_reason: str | None = None,
    ) -> SavedDraft:
        draft = StructuredDraft.from_json(draft_payload)
        if correction_reason is not None and not correction_reason.strip():
            raise ValueError("Correction reason cannot be blank")

        expired = False
        saved: SavedDraft | None = None
        with self._sessions.begin() as session:
            professional = session.get(Professional, professional_id)
            if professional is None or not professional.active:
                raise AuthenticationError("Professional access is not active")
            clinical_session = _owned_session(session, professional.id, session_id, lock=True)
            if clinical_session.status not in {"TRANSCRIBED", "DRAFT", "APPROVED"}:
                raise DraftRejected("Session cannot accept a draft in its current state")
            if clinical_session.status == "APPROVED" and correction_reason is None:
                raise DraftRejected("A reason is required to correct an approved session")
            if draft.patient_id != clinical_session.patient_id:
                raise DraftRejected("Draft patient does not match the session")
            if draft.session_date != clinical_session.session_date:
                raise DraftRejected("Draft date does not match the session")
            if draft.template != clinical_session.template:
                raise DraftRejected("Draft template does not match the session")
            if _as_utc(clinical_session.transcript_expires_at) <= utc_now():
                clinical_session.transcript_enc = b""
                expired = True
                _audit(
                    session,
                    professional.id,
                    "transcript.expired",
                    "clinical_session",
                    session_id,
                )
            else:
                transcript = self._cipher.decrypt_bytes(clinical_session.transcript_enc).decode(
                    "utf-8"
                )
                fidelity = verify_draft_fidelity(draft, transcript)
                canonical_content = _canonical_json(draft.model_dump(mode="json"))
                previous_version = session.scalar(
                    select(func.max(SessionVersion.version)).where(
                        SessionVersion.session_id == clinical_session.id
                    )
                )
                version_number = (previous_version or 0) + 1
                version_id = new_id()
                version = SessionVersion(
                    id=version_id,
                    session_id=clinical_session.id,
                    version=version_number,
                    content_enc=self._cipher.encrypt_bytes(canonical_content),
                    content_hash=hashlib.sha256(canonical_content).hexdigest(),
                    fidelity_passed=fidelity.grounded,
                    fidelity_issues_enc=self._cipher.encrypt_bytes(
                        _canonical_json(
                            [{"code": issue.code, "path": issue.path} for issue in fidelity.issues]
                        )
                    ),
                    correction_reason_enc=(
                        self._cipher.encrypt_bytes(correction_reason.strip().encode("utf-8"))
                        if correction_reason is not None
                        else None
                    ),
                    created_by=professional.id,
                )
                session.add(version)
                clinical_session.current_version_id = version_id
                clinical_session.status = "DRAFT"
                action = (
                    "session.correction_drafted" if correction_reason else "session.draft_saved"
                )
                _audit(session, professional.id, action, "session_version", version_id)
                saved = SavedDraft(
                    session_id=clinical_session.id,
                    version_id=version_id,
                    version=version_number,
                    fidelity=fidelity,
                )
        if expired:
            raise TranscriptExpired("Transcript retention period has expired")
        if saved is None:
            raise IntegrityViolation("Draft save did not produce a version")
        return saved

    def approve(self, chat_id: str, session_id: str) -> ApprovalResult:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            clinical_session = _owned_session(session, professional.id, session_id, lock=True)
            if clinical_session.status != "DRAFT" or clinical_session.current_version_id is None:
                raise DraftRejected("Only the current draft can be approved")
            version = session.scalar(
                select(SessionVersion)
                .where(
                    SessionVersion.id == clinical_session.current_version_id,
                    SessionVersion.session_id == clinical_session.id,
                    SessionVersion.approved_at.is_(None),
                )
                .with_for_update()
            )
            if version is None:
                raise DraftRejected("Current draft version is unavailable")
            if not version.fidelity_passed:
                raise DraftRejected("Draft contains unsupported or unverified claims")
            draft = self._load_version(version)
            patient = session.get(Patient, clinical_session.patient_id)
            if patient is None:
                raise ResourceNotFound("Patient not found")
            patient_name = self._cipher.decrypt(patient.display_name_enc)
            if patient_name is None:
                raise IntegrityViolation("Patient display name is unavailable")
            rectification_reason = (
                self._cipher.decrypt_bytes(version.correction_reason_enc).decode("utf-8")
                if version.correction_reason_enc is not None
                else None
            )
            pdf_bytes = self._pdf_renderer.render(
                draft,
                PDFIdentity(
                    patient_name=patient_name,
                    professional_name=self._cipher.decrypt(professional.name_enc) or "",
                    profession=self._cipher.decrypt(professional.profession_enc) or "",
                    council_registry=self._cipher.decrypt(professional.council_registry_enc) or "",
                ),
                is_rectification=version.version > 1,
                rectification_reason=rectification_reason,
            )
            version.approved_at = utc_now()
            clinical_session.status = "APPROVED"
            document_id = new_id()
            session.add(
                ClinicalDocument(
                    id=document_id,
                    session_version_id=version.id,
                    document_type="evolution",
                    content_enc=self._cipher.encrypt_bytes(pdf_bytes),
                    content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
                )
            )
            _audit(session, professional.id, "session.approved", "session_version", version.id)
            return ApprovalResult(
                session_id=clinical_session.id,
                version_id=version.id,
                document_id=document_id,
                pdf=pdf_bytes,
            )

    def history(
        self,
        chat_id: str,
        patient_id: str,
        *,
        limit: int = 10,
    ) -> list[SessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            patient = session.scalar(
                select(Patient).where(
                    Patient.id == patient_id,
                    Patient.professional_id == professional.id,
                    Patient.active.is_(True),
                )
            )
            if patient is None:
                raise ResourceNotFound("Patient not found")
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.patient_id == patient_id,
                    ClinicalSession.professional_id == professional.id,
                    ClinicalSession.status == "APPROVED",
                )
                .order_by(ClinicalSession.session_date.desc(), ClinicalSession.created_at.desc())
                .limit(limit)
            ).all()
            summaries = [self._summary(session, record) for record in records]
            _audit(session, professional.id, "session.history_listed", "patient", patient_id)
            return summaries

    def get_pdf(
        self,
        chat_id: str,
        session_id: str,
        *,
        version_number: int | None = None,
    ) -> bytes:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            clinical_session = _owned_session(session, professional.id, session_id)
            version_query = select(SessionVersion).where(
                SessionVersion.session_id == clinical_session.id,
                SessionVersion.approved_at.is_not(None),
            )
            if version_number is None:
                version_query = version_query.order_by(SessionVersion.version.desc()).limit(1)
            else:
                version_query = version_query.where(SessionVersion.version == version_number)
            version = session.scalar(version_query)
            if version is None:
                raise ResourceNotFound("Approved version not found")
            document = session.scalar(
                select(ClinicalDocument).where(
                    ClinicalDocument.session_version_id == version.id,
                    ClinicalDocument.document_type == "evolution",
                )
            )
            if document is None:
                raise ResourceNotFound("Approved PDF not found")
            pdf_bytes = self._cipher.decrypt_bytes(document.content_enc)
            if hashlib.sha256(pdf_bytes).hexdigest() != document.content_hash:
                raise IntegrityViolation("Approved PDF integrity check failed")
            _audit(
                session,
                professional.id,
                "document.read",
                "clinical_document",
                document.id,
            )
            return pdf_bytes

    def get_session_draft(self, chat_id: str, session_id: str) -> StructuredDraft:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            clinical_session = _owned_session(session, professional.id, session_id)
            if clinical_session.current_version_id is None:
                raise ResourceNotFound("Session has no saved draft version")
            version = session.get(SessionVersion, clinical_session.current_version_id)
            if version is None:
                raise ResourceNotFound("Current session version not found")
            return self._load_version(version)

    def discard_session(self, chat_id: str, session_id: str) -> None:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            clinical_session = _owned_session(session, professional.id, session_id, lock=True)
            if clinical_session.status == "APPROVED":
                if self._minimum_retention_days > 0:
                    min_deadline = _as_utc(clinical_session.created_at) + timedelta(
                        days=self._minimum_retention_days
                    )
                    if utc_now() < min_deadline:
                        raise DraftRejected(
                            "Approved session cannot be discarded before minimum legal retention"
                        )
                raise DraftRejected("Approved sessions cannot be discarded; submit a rectification")
            clinical_session.status = "DISCARDED"
            clinical_session.transcript_enc = b""
            _audit(
                session,
                professional.id,
                "session.discarded",
                "clinical_session",
                clinical_session.id,
            )

    def list_pending_drafts_for_reminder(
        self,
        *,
        older_than_hours: int = 24,
        limit: int = 100,
    ) -> list[TranscribedSession]:
        if older_than_hours <= 0:
            raise ValueError("Reminder threshold must be positive")
        cutoff = utc_now() - timedelta(hours=older_than_hours)
        with self._sessions.begin() as session:
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.status.in_(("TRANSCRIBED", "DRAFT")),
                    ClinicalSession.created_at <= cutoff,
                )
                .order_by(ClinicalSession.created_at)
                .limit(limit)
            ).all()
            return [_session_record(record) for record in records]

    def archive_stale_drafts(self, *, older_than_days: int = 7, limit: int = 100) -> int:
        if older_than_days <= 0:
            raise ValueError("Archive threshold must be positive")
        cutoff = utc_now() - timedelta(days=older_than_days)
        archived = 0
        with self._sessions.begin() as session:
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.status.in_(("TRANSCRIBED", "DRAFT")),
                    ClinicalSession.created_at <= cutoff,
                )
                .order_by(ClinicalSession.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for record in records:
                record.status = "ARCHIVED"
                _audit(
                    session,
                    record.professional_id,
                    "session.archived",
                    "clinical_session",
                    record.id,
                )
                archived += 1
        return archived

    def search_sessions(
        self,
        chat_id: str,
        query: str,
        *,
        patient_id: str | None = None,
        limit: int = 20,
    ) -> list[SearchMatch]:
        needle = " ".join(query.split()).casefold()
        if len(needle) < 2:
            raise ValueError("Search query must have at least 2 characters")
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            stmt = select(ClinicalSession).where(
                ClinicalSession.professional_id == professional.id,
                ClinicalSession.status != "DISCARDED",
            )
            if patient_id is not None:
                stmt = stmt.where(ClinicalSession.patient_id == patient_id)
            stmt = stmt.order_by(
                ClinicalSession.session_date.desc(), ClinicalSession.created_at.desc()
            )
            records = session.scalars(stmt).all()

            matches: list[SearchMatch] = []
            for record in records:
                searchable_chunks: list[str] = []
                if record.current_version_id is not None:
                    version = session.get(SessionVersion, record.current_version_id)
                    if version is not None:
                        draft = self._load_version(version)
                        searchable_chunks.extend(_draft_searchable_texts(draft))
                if record.transcript_enc and _as_utc(record.transcript_expires_at) > utc_now():
                    searchable_chunks.append(
                        self._cipher.decrypt_bytes(record.transcript_enc).decode("utf-8")
                    )
                combined = " ".join(searchable_chunks)
                pos = combined.casefold().find(needle)
                if pos != -1:
                    start = max(0, pos - 30)
                    end = min(len(combined), pos + len(needle) + 60)
                    matches.append(
                        SearchMatch(
                            session_id=record.id,
                            patient_id=record.patient_id,
                            session_date=record.session_date,
                            status=_session_status(record.status),
                            snippet=combined[start:end].strip(),
                        )
                    )
                    if len(matches) >= limit:
                        break
            _audit(session, professional.id, "session.searched", "professional", professional.id)
            return matches

    def goal_progress(
        self,
        chat_id: str,
        patient_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[GoalProgressSeries]:
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            patient = session.scalar(
                select(Patient).where(
                    Patient.id == patient_id,
                    Patient.professional_id == professional.id,
                    Patient.active.is_(True),
                )
            )
            if patient is None:
                raise ResourceNotFound("Patient not found")
            goals = session.scalars(select(Goal).where(Goal.patient_id == patient_id)).all()
            stmt = select(ClinicalSession).where(
                ClinicalSession.patient_id == patient_id,
                ClinicalSession.professional_id == professional.id,
                ClinicalSession.status == "APPROVED",
            )
            if start_date is not None:
                stmt = stmt.where(ClinicalSession.session_date >= start_date)
            if end_date is not None:
                stmt = stmt.where(ClinicalSession.session_date <= end_date)
            sessions = session.scalars(
                stmt.order_by(ClinicalSession.session_date.asc(), ClinicalSession.created_at.asc())
            ).all()

            points_by_goal: dict[str, list[GoalMeasurePoint]] = {goal.id: [] for goal in goals}
            for clinical_session in sessions:
                if clinical_session.current_version_id is None:
                    continue
                version = session.get(SessionVersion, clinical_session.current_version_id)
                if version is None or version.approved_at is None:
                    continue
                draft = self._load_version(version)
                for measure in draft.content.measures:
                    if measure.goal_id and measure.goal_id in points_by_goal:
                        points_by_goal[measure.goal_id].append(
                            GoalMeasurePoint(
                                session_id=clinical_session.id,
                                session_date=clinical_session.session_date,
                                value=measure.value,
                                unit=measure.unit,
                                is_approximate=measure.is_approximate,
                                original_expression=measure.original_expression,
                            )
                        )

            result: list[GoalProgressSeries] = []
            for goal in goals:
                pts = tuple(points_by_goal[goal.id])
                result.append(
                    GoalProgressSeries(
                        goal_id=goal.id,
                        description=self._cipher.decrypt(goal.description_enc) or "",
                        indicator_type=goal.indicator_type,
                        mastery_criterion=self._cipher.decrypt(goal.mastery_criterion_enc) or "",
                        points=pts,
                        trend_summary=_summarize_trend(pts),
                    )
                )
            _audit(session, professional.id, "goal.progress_read", "patient", patient_id)
            return result

    def build_periodic_report_pdf(
        self,
        chat_id: str,
        patient_id: str,
        *,
        start_date: date,
        end_date: date,
    ) -> bytes:
        if end_date < start_date:
            raise ValueError("End date cannot be earlier than start date")
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            patient = session.scalar(
                select(Patient).where(
                    Patient.id == patient_id,
                    Patient.professional_id == professional.id,
                    Patient.active.is_(True),
                )
            )
            if patient is None:
                raise ResourceNotFound("Patient not found")
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.patient_id == patient_id,
                    ClinicalSession.professional_id == professional.id,
                    ClinicalSession.status == "APPROVED",
                    ClinicalSession.session_date >= start_date,
                    ClinicalSession.session_date <= end_date,
                )
                .order_by(ClinicalSession.session_date.asc(), ClinicalSession.created_at.asc())
            ).all()
            if not records:
                raise ResourceNotFound("No approved sessions found in the requested period")

            loaded: list[tuple[int, StructuredDraft]] = []
            for record in records:
                if record.current_version_id is None:
                    continue
                version = session.get(SessionVersion, record.current_version_id)
                if version is None or version.approved_at is None:
                    continue
                loaded.append((version.version, self._load_version(version)))
            patient_name = self._cipher.decrypt(patient.display_name_enc) or ""
            identity = PDFIdentity(
                patient_name=patient_name,
                professional_name=self._cipher.decrypt(professional.name_enc) or "",
                profession=self._cipher.decrypt(professional.profession_enc) or "",
                council_registry=self._cipher.decrypt(professional.council_registry_enc) or "",
            )
        series = self.goal_progress(chat_id, patient_id, start_date=start_date, end_date=end_date)
        goal_summaries = [
            f"{item.description} ({item.mastery_criterion}): {item.trend_summary}"
            for item in series
        ]
        pdf_bytes = self._pdf_renderer.render_periodic_report(
            identity,
            start_date=start_date,
            end_date=end_date,
            sessions=loaded,
            goal_summaries=goal_summaries,
        )
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            _audit(session, professional.id, "report.periodic_generated", "patient", patient_id)
        return pdf_bytes

    def export_patient_bundle(self, chat_id: str, patient_id: str) -> bytes:
        """Export approved evolutions (JSON + PDFs) for a patient as a ZIP archive (RF25)."""
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            patient = session.scalar(
                select(Patient).where(
                    Patient.id == patient_id,
                    Patient.professional_id == professional.id,
                    Patient.active.is_(True),
                )
            )
            if patient is None:
                raise ResourceNotFound("Patient not found")
            records = session.scalars(
                select(ClinicalSession)
                .where(
                    ClinicalSession.patient_id == patient_id,
                    ClinicalSession.professional_id == professional.id,
                    ClinicalSession.status == "APPROVED",
                )
                .order_by(ClinicalSession.session_date.asc(), ClinicalSession.created_at.asc())
            ).all()

            zip_buffer = BytesIO()
            manifest_sessions: list[dict[str, object]] = []
            with ZipFile(zip_buffer, "w", compression=ZIP_DEFLATED) as archive:
                for record in records:
                    if record.current_version_id is None:
                        continue
                    version = session.get(SessionVersion, record.current_version_id)
                    if version is None or version.approved_at is None:
                        continue
                    draft = self._load_version(version)
                    manifest_sessions.append(
                        {
                            "session_id": record.id,
                            "session_date": record.session_date.isoformat(),
                            "template": record.template,
                            "version": version.version,
                            "content_hash": version.content_hash,
                            "draft": draft.model_dump(mode="json"),
                        }
                    )
                    doc = session.scalar(
                        select(ClinicalDocument).where(
                            ClinicalDocument.session_version_id == version.id,
                            ClinicalDocument.document_type == "evolution",
                        )
                    )
                    if doc is not None:
                        pdf_bytes = self._cipher.decrypt_bytes(doc.content_enc)
                        if hashlib.sha256(pdf_bytes).hexdigest() != doc.content_hash:
                            raise IntegrityViolation("Approved PDF integrity check failed")
                        archive.writestr(
                            f"pdfs/{record.session_date.isoformat()}_{record.id[:8]}_v{version.version}.pdf",
                            pdf_bytes,
                        )
                manifest = {
                    "patient_id": patient.id,
                    "patient_name": self._cipher.decrypt(patient.display_name_enc) or "",
                    "exported_at": utc_now().isoformat(),
                    "sessions": manifest_sessions,
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            _audit(session, professional.id, "patient.exported", "patient", patient_id)
            return zip_buffer.getvalue()

    def list_audit_events(self, chat_id: str, *, limit: int = 50) -> list[AuditEventRecord]:
        if not 1 <= limit <= 200:
            raise ValueError("Audit limit must be between 1 and 200")
        with self._sessions.begin() as session:
            professional = _require_professional(session, self._cipher, chat_id)
            rows = session.scalars(
                select(AuditLog)
                .where(AuditLog.actor_id == professional.id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            ).all()
            return [
                AuditEventRecord(
                    id=row.id,
                    action=row.action,
                    object_type=row.object_type,
                    object_id=row.object_id,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def _load_version(self, version: SessionVersion) -> StructuredDraft:
        raw_content = self._cipher.decrypt_bytes(version.content_enc)
        if hashlib.sha256(raw_content).hexdigest() != version.content_hash:
            raise IntegrityViolation("Session version integrity check failed")
        return StructuredDraft.from_json(raw_content)

    def _summary(self, session: Session, record: ClinicalSession) -> SessionSummary:
        if record.current_version_id is None:
            raise IntegrityViolation("Approved session has no current version")
        version = session.get(SessionVersion, record.current_version_id)
        if version is None or version.approved_at is None:
            raise IntegrityViolation("Approved session version is unavailable")
        draft = self._load_version(version)
        title = _draft_title(draft)
        return SessionSummary(
            id=record.id,
            patient_id=record.patient_id,
            session_date=record.session_date,
            template=record.template,
            version=version.version,
            title=title,
        )


def _require_professional(session: Session, cipher: FieldCipher, chat_id: str) -> Professional:
    chat_id_digest = cipher.digest(chat_id)
    professional = session.scalar(
        select(Professional).where(
            Professional.chat_id_digest == chat_id_digest,
            Professional.active.is_(True),
        )
    )
    if professional is None:
        raise AuthenticationError("Professional access is not active")
    return professional


def _owned_session(
    session: Session,
    professional_id: str,
    session_id: str,
    *,
    lock: bool = False,
) -> ClinicalSession:
    query = select(ClinicalSession).where(
        ClinicalSession.id == session_id,
        ClinicalSession.professional_id == professional_id,
    )
    if lock:
        query = query.with_for_update()
    clinical_session = session.scalar(query)
    if clinical_session is None:
        raise ResourceNotFound("Session not found")
    return clinical_session


def _audit(
    session: Session,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
) -> None:
    session.add(
        AuditLog(
            id=new_id(),
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
        )
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _session_record(record: ClinicalSession) -> TranscribedSession:
    return TranscribedSession(
        id=record.id,
        patient_id=record.patient_id,
        session_date=record.session_date,
        template=record.template,
        status=_session_status(record.status),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _draft_title(draft: StructuredDraft) -> str:
    match draft.content:
        case SOAPContent():
            claims = draft.content.parent_summary or draft.content.objective
        case ABCContent():
            claims = draft.content.parent_summary
        case FreeContent():
            claims = draft.content.parent_summary or tuple(
                claim for section in draft.content.sections for claim in section.claims
            )
    if not claims:
        return "Evolução aprovada"
    title = " ".join(claims[0].text.split())
    return title[:120]


def _draft_searchable_texts(draft: StructuredDraft) -> list[str]:
    texts: list[str] = []
    match draft.content:
        case SOAPContent():
            for group in (
                draft.content.subjective,
                draft.content.objective,
                draft.content.assessment,
                draft.content.plan,
                draft.content.parent_summary,
            ):
                texts.extend(claim.text for claim in group)
        case ABCContent():
            for episode in draft.content.episodes:
                texts.extend(
                    [episode.antecedent.text, episode.behavior.text, episode.consequence.text]
                )
                if episode.hypothesized_function is not None:
                    texts.append(episode.hypothesized_function.text)
            texts.extend(claim.text for claim in draft.content.parent_summary)
        case FreeContent():
            for section in draft.content.sections:
                texts.append(section.title)
                texts.extend(claim.text for claim in section.claims)
            texts.extend(claim.text for claim in draft.content.parent_summary)
    return texts


def _summarize_trend(points: tuple[GoalMeasurePoint, ...]) -> str:
    if not points:
        return "Sem medições aprovadas no período."
    if len(points) == 1:
        p = points[0]
        return f"1 medição ({p.value} {p.unit} em {p.session_date:%d/%m/%Y})."
    first, last = points[0], points[-1]
    delta = last.value - first.value
    direction = "estável"
    if delta > 0:
        direction = f"+{delta.normalize()}"
    elif delta < 0:
        direction = f"{delta.normalize()}"
    return (
        f"{len(points)} medições: {first.value} -> {last.value} {last.unit} (variação {direction})."
    )


def _session_status(value: str) -> SessionStatus:
    match value:
        case "TRANSCRIBED":
            return "TRANSCRIBED"
        case "DRAFT":
            return "DRAFT"
        case "APPROVED":
            return "APPROVED"
        case "ARCHIVED":
            return "ARCHIVED"
        case "DISCARDED":
            return "DISCARDED"
        case _:
            raise IntegrityViolation("Persisted session has an unsupported status")
