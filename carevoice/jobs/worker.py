"""Asynchronous worker for consent-checked transcription jobs."""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Literal

import structlog

from carevoice.config import Settings
from carevoice.db.database import Database
from carevoice.db.models import Goal, Guardian, Patient, Professional
from carevoice.domain.identity import ConsentRequired, IdentityService
from carevoice.domain.records import ClinicalRecordService, TranscribedSession
from carevoice.jobs.queue import ClaimedJob, InvalidJobTransition, JobErrorCode, JobQueue
from carevoice.logging import configure_logging
from carevoice.security.crypto import FieldCipher
from carevoice.structure.dates import resolve_session_date
from carevoice.structure.llm import ClinicalStructurer, StructuringContext, UrllibLLMTransport
from carevoice.structure.schemas import Template
from carevoice.transcribe.base import AudioFormat, Transcriber

WorkerStatus = Literal["idle", "completed", "failed"]
_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WorkerOutcome:
    status: WorkerStatus
    job_id: str | None = None
    session: TranscribedSession | None = None
    error_code: JobErrorCode | None = None


class ProcessingWorker:
    """Runs jobs without blocking the event loop or logging clinical content."""

    def __init__(
        self,
        queue: JobQueue,
        records: ClinicalRecordService,
        transcriber: Transcriber,
        *,
        structurer: ClinicalStructurer | None = None,
        database: Database | None = None,
        cipher: FieldCipher | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Worker polling interval must be positive")
        self._queue = queue
        self._records = records
        self._transcriber = transcriber
        self._structurer = structurer
        self._database = database
        self._cipher = cipher
        self._poll_interval_seconds = poll_interval_seconds
        self._last_transcript_cleanup = 0.0

    async def process_once(self) -> WorkerOutcome:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_transcript_cleanup >= 60:
            purged = await asyncio.to_thread(self._records.purge_expired_transcripts)
            self._last_transcript_cleanup = now
            if purged:
                _logger.info("expired_transcripts_purged", count=purged)
        job = await asyncio.to_thread(self._queue.claim_next)
        if job is None:
            return WorkerOutcome(status="idle")

        try:
            return await self._process_claimed(job)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(asyncio.to_thread(self._queue.fail, job.id, "internal_error"))
            except InvalidJobTransition:
                _logger.warning("processing_job_already_transitioned", job_id=job.id)
            raise

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            outcome = await self.process_once()
            if outcome.status == "idle":
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    continue

    async def _process_claimed(self, job: ClaimedJob) -> WorkerOutcome:
        try:
            await asyncio.to_thread(self._queue.assert_claim_is_authorized, job.id)
        except ConsentRequired:
            return await self._reject_revoked_consent(job, already_failed=True)

        try:
            transcript = await self._transcribe(job)
        except (UnicodeDecodeError, ValueError) as exc:
            return await self._mark_failed(job, "invalid_payload", exc)
        except Exception as exc:
            return await self._mark_failed(job, "transcription_failed", exc)

        if not transcript.strip():
            return await self._mark_failed(job, "invalid_payload", None)

        try:
            await asyncio.to_thread(self._queue.assert_claim_is_authorized, job.id)
        except ConsentRequired:
            return await self._reject_revoked_consent(job, already_failed=True)

        try:
            resolved_date = await asyncio.to_thread(self._resolve_date_for_job, job, transcript)
            clinical_session = await asyncio.to_thread(
                self._records.create_transcribed_session,
                job_id=job.id,
                professional_id=job.professional_id,
                patient_id=job.patient_id,
                transcript=transcript,
                session_date=resolved_date,
            )
        except ConsentRequired:
            return await self._reject_revoked_consent(job, already_failed=False)
        except Exception as exc:
            return await self._mark_failed(job, "internal_error", exc)

        if self._structurer is not None:
            await self._try_structure_session(job, clinical_session, transcript)

        try:
            await asyncio.to_thread(self._queue.complete, job.id)
        except ConsentRequired:
            return await self._reject_revoked_consent(job, already_failed=True)
        except Exception as exc:
            return await self._mark_failed(job, "internal_error", exc)

        _logger.info("processing_job_completed", job_id=job.id, session_id=clinical_session.id)
        return WorkerOutcome(status="completed", job_id=job.id, session=clinical_session)

    async def _try_structure_session(
        self,
        job: ClaimedJob,
        clinical_session: TranscribedSession,
        transcript: str,
    ) -> None:
        if self._structurer is None or self._database is None or self._cipher is None:
            return
        try:
            context = await asyncio.to_thread(
                self._load_structuring_context,
                job,
                clinical_session,
            )
            outcome = await self._structurer.structure(transcript, context)
            await asyncio.to_thread(
                self._records.save_draft_for_professional,
                job.professional_id,
                clinical_session.id,
                outcome.draft.model_dump_json(),
            )
        except Exception as exc:
            _logger.warning(
                "session_auto_structuring_skipped",
                job_id=job.id,
                session_id=clinical_session.id,
                error_type=type(exc).__name__,
            )

    def _resolve_date_for_job(self, job: ClaimedJob, transcript: str) -> date | None:
        if self._database is None:
            return None
        from sqlalchemy import select

        with self._database.sessions() as session:
            timezone = session.scalar(
                select(Professional.timezone).where(Professional.id == job.professional_id)
            )
        if not timezone:
            return None
        return resolve_session_date(transcript, timezone_name=timezone)

    def _load_structuring_context(
        self,
        job: ClaimedJob,
        clinical_session: TranscribedSession,
    ) -> StructuringContext:
        assert self._database is not None and self._cipher is not None
        from sqlalchemy import select

        with self._database.sessions() as session:
            patient = session.get(Patient, job.patient_id)
            if patient is None:
                raise ValueError("Patient not found")
            guardians = session.scalars(
                select(Guardian).where(Guardian.patient_id == job.patient_id)
            ).all()
            goals = session.scalars(
                select(Goal).where(Goal.patient_id == job.patient_id, Goal.status == "active")
            ).all()
            patient_name = self._cipher.decrypt(patient.display_name_enc) or ""
            patient_alias = self._cipher.decrypt(patient.alias_enc)
            guardian_names = tuple(
                name
                for g in guardians
                if (name := self._cipher.decrypt(g.name_enc)) is not None and name.strip()
            )
            goal_lines = tuple(
                f"[{g.id}] {self._cipher.decrypt(g.description_enc) or ''} "
                f"(indicador: {g.indicator_type}, critério: "
                f"{self._cipher.decrypt(g.mastery_criterion_enc) or ''})"
                for g in goals
            )
        template_value: Template
        match clinical_session.template:
            case "SOAP" | "ABC" | "FREE":
                template_value = clinical_session.template
            case _:
                template_value = "SOAP"
        return StructuringContext(
            patient_id=clinical_session.patient_id,
            patient_name=patient_name,
            patient_alias=patient_alias,
            guardian_names=guardian_names,
            session_date=clinical_session.session_date,
            template=template_value,
            active_goals=goal_lines,
        )

    async def _transcribe(self, job: ClaimedJob) -> str:
        if job.kind == "text":
            return job.payload.decode("utf-8").strip()
        match job.audio_format:
            case "ogg" | "mp3" | "m4a" | "wav":
                audio_format: AudioFormat = job.audio_format
            case _:
                raise ValueError("Audio job has no supported format")
        result = await self._transcriber.transcribe(job.payload, audio_format)
        return result.text.strip()

    async def _reject_revoked_consent(
        self,
        job: ClaimedJob,
        *,
        already_failed: bool,
    ) -> WorkerOutcome:
        if not already_failed:
            await asyncio.to_thread(self._queue.fail, job.id, "consent_revoked")
        await asyncio.to_thread(self._records.discard_transcription, job.id)
        _logger.warning("processing_job_rejected", job_id=job.id, error_code="consent_revoked")
        return WorkerOutcome(
            status="failed",
            job_id=job.id,
            error_code="consent_revoked",
        )

    async def _mark_failed(
        self,
        job: ClaimedJob,
        error_code: JobErrorCode,
        error: Exception | None,
    ) -> WorkerOutcome:
        retry_scheduled = await asyncio.to_thread(self._queue.fail, job.id, error_code)
        if not retry_scheduled:
            await asyncio.to_thread(self._records.discard_transcription, job.id)
        _logger.warning(
            "processing_job_failed",
            job_id=job.id,
            error_code=error_code,
            error_type=type(error).__name__ if error is not None else None,
            retry_scheduled=retry_scheduled,
        )
        return WorkerOutcome(
            status="failed",
            job_id=job.id,
            error_code=error_code,
        )


def main() -> None:
    """Run the processing worker using environment-backed application settings."""
    settings = Settings()
    if settings.field_encryption_key is None:
        raise ValueError("CAREVOICE_FIELD_ENCRYPTION_KEY is required to run the worker")
    if settings.administrator_chat_id is None:
        raise ValueError("CAREVOICE_ADMINISTRATOR_CHAT_ID is required to run the worker")

    configure_logging()
    database = Database(settings.database_url.get_secret_value())
    try:
        cipher = FieldCipher(settings.field_encryption_key.get_secret_value())
        identity = IdentityService(
            database.sessions,
            cipher,
            settings.administrator_chat_id.get_secret_value(),
        )
        queue = JobQueue(
            database.sessions,
            identity,
            cipher,
            max_audio_mb=settings.max_audio_mb,
            max_audio_seconds=settings.max_audio_seconds,
            daily_limit=settings.daily_jobs_limit,
        )
        records = ClinicalRecordService(
            database.sessions,
            cipher,
            transcript_retention_days=settings.transcript_retention_days,
            minimum_retention_days=settings.minimum_retention_days,
        )
        transcriber: Transcriber
        if settings.transcriber == "cloud":
            from carevoice.transcribe.cloud import CloudOpenAITranscriber

            transcriber = CloudOpenAITranscriber(
                cloud_processing_enabled=settings.cloud_processing_enabled,
                model=settings.cloud_transcriber_model,
                base_url=settings.cloud_transcriber_base_url,
                api_key=(
                    settings.cloud_transcriber_api_key.get_secret_value()
                    if settings.cloud_transcriber_api_key
                    else None
                ),
            )
        else:
            from carevoice.transcribe.local_whisper import FasterWhisperTranscriber

            transcriber = FasterWhisperTranscriber()

        llm_transport = UrllibLLMTransport(
            provider=settings.llm_provider,
            base_url=(
                settings.cloud_llm_base_url
                if settings.llm_provider == "cloud"
                else settings.ollama_base_url
            ),
            model=settings.llm_model,
            api_key=(
                settings.cloud_llm_api_key.get_secret_value()
                if settings.cloud_llm_api_key
                else None
            ),
            cloud_processing_enabled=settings.cloud_processing_enabled,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        structurer = ClinicalStructurer(
            llm_transport,
            pseudonymize_before_llm=settings.pseudonymize_before_llm,
        )
        worker = ProcessingWorker(
            queue,
            records,
            transcriber,
            structurer=structurer,
            database=database,
            cipher=cipher,
        )
        asyncio.run(worker.run(asyncio.Event()))
    finally:
        database.dispose()
