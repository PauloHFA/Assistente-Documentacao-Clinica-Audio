import asyncio
import json
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from datetime import date

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from carevoice.db.database import Database
from carevoice.db.models import ClinicalSession, ProcessingJob, SessionVersion
from carevoice.documents.pdf import PDFIdentity, PDFRenderer
from carevoice.domain.identity import ConsentRequired, IdentityService, ResourceNotFound
from carevoice.domain.records import ClinicalRecordService, DraftRejected, TranscriptExpired
from carevoice.jobs.queue import JobQueue
from carevoice.jobs.worker import ProcessingWorker, WorkerOutcome, main
from carevoice.security.crypto import FieldCipher
from carevoice.structure.schemas import StructuredDraft
from carevoice.transcribe.base import AudioFormat, TranscriptionResult

ADMIN_CHAT_ID = "admin-chat"
TRANSCRIPT = "A mãe relatou melhora e descreveu 80% de acerto durante a atividade."


@dataclass
class FakeTranscriber:
    text: str = TRANSCRIPT
    on_transcribe: Callable[[], None] | None = None
    error: Exception | None = None
    calls: list[tuple[bytes, AudioFormat, Sequence[str]]] | None = None

    async def transcribe(
        self,
        payload: bytes,
        audio_format: AudioFormat,
        glossary_terms: Sequence[str] = (),
    ) -> TranscriptionResult:
        if self.calls is None:
            self.calls = []
        self.calls.append((payload, audio_format, glossary_terms))
        if callable(self.on_transcribe):
            self.on_transcribe()
        if self.error is not None:
            raise self.error
        return TranscriptionResult(text=self.text, language="pt")


@pytest.fixture
def worker_context() -> Generator[
    tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
    None,
    None,
]:
    database = Database("sqlite:///:memory:")
    database.create_schema_for_tests()
    cipher = FieldCipher(Fernet.generate_key().decode("ascii"))
    identity = IdentityService(database.sessions, cipher, ADMIN_CHAT_ID)
    invitation = identity.create_invitation(
        ADMIN_CHAT_ID,
        name="Terapeuta",
        profession="Fonoaudióloga",
        council_registry="REG-123",
    )
    identity.consume_invitation(invitation, "chat-one")
    patient = identity.create_patient("chat-one", display_name="Paciente & <Teste>")
    identity.grant_consent(
        "chat-one",
        patient.id,
        terms_version="v1",
        purpose="processamento local",
    )
    queue = JobQueue(database.sessions, identity, cipher, base_retry_seconds=1)
    records = ClinicalRecordService(database.sessions, cipher)
    yield identity, queue, records, database, patient.id
    database.dispose()


def test_text_job_transcribes_saves_approves_and_recovers_pdf(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    job_id = queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    worker = ProcessingWorker(queue, records, FakeTranscriber())

    outcome = asyncio.run(worker.process_once())

    assert outcome.status == "completed"
    assert outcome.job_id == job_id
    assert outcome.session is not None
    assert records.get_transcript("chat-one", outcome.session.id) == TRANSCRIPT
    saved = records.save_draft(
        "chat-one",
        outcome.session.id,
        _draft_json(patient_id, outcome.session.session_date),
    )
    assert saved.fidelity.grounded is True
    approved = records.approve("chat-one", outcome.session.id)
    assert approved.pdf.startswith(b"%PDF-")
    assert records.get_pdf("chat-one", outcome.session.id) == approved.pdf
    history = records.history("chat-one", patient_id)
    assert len(history) == 1
    assert history[0].version == 1

    with database.sessions() as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.payload_enc == b""


def test_audio_job_passes_original_format_to_injected_transcriber(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, _, patient_id = worker_context
    payload = b"fictitious audio bytes"
    queue.enqueue_audio(
        "chat-one",
        patient_id,
        filename="recording.ogg",
        payload=payload,
        duration_seconds=10,
        content_type="audio/ogg",
    )
    transcriber = FakeTranscriber()

    outcome = asyncio.run(ProcessingWorker(queue, records, transcriber).process_once())

    assert outcome.status == "completed"
    assert transcriber.calls == [(payload, "ogg", ())]


def test_consent_revocation_during_transcription_discards_transcript(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    identity, queue, records, database, patient_id = worker_context
    job_id = queue.enqueue_audio(
        "chat-one",
        patient_id,
        filename="recording.wav",
        payload=b"fictitious audio bytes",
        duration_seconds=10,
        content_type="audio/wav",
    )
    transcriber = FakeTranscriber(
        on_transcribe=lambda: identity.revoke_consent("chat-one", patient_id)
    )

    outcome = asyncio.run(ProcessingWorker(queue, records, transcriber).process_once())

    assert outcome.status == "failed"
    assert outcome.error_code == "consent_revoked"
    with database.sessions() as session:
        job = session.get(ProcessingJob, job_id)
        clinical_session = session.scalar(
            select(ClinicalSession).where(ClinicalSession.job_id == job_id)
        )
        assert job is not None
        assert job.status == "failed"
        assert job.payload_enc == b""
        assert clinical_session is None


def test_unsupported_claim_is_saved_but_cannot_be_approved(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, _, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    payload = json.loads(_draft_json(patient_id, outcome.session.session_date))
    payload["content"]["objective"][0]["text"] = "Obteve 90% de acerto."
    payload["content"]["objective"][0]["evidence"][0]["quote"] = "A mãe relatou melhora."
    saved = records.save_draft("chat-one", outcome.session.id, json.dumps(payload))
    assert saved.fidelity.grounded is False
    with pytest.raises(DraftRejected, match="unsupported"):
        records.approve("chat-one", outcome.session.id)


def test_session_access_remains_scoped_to_linked_professional(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    identity, queue, records, _, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    invitation = identity.create_invitation(
        ADMIN_CHAT_ID,
        name="Outro profissional",
        profession="Fonoaudiólogo",
        council_registry="REG-456",
    )
    identity.consume_invitation(invitation, "chat-two")

    with pytest.raises(ResourceNotFound):
        records.get_transcript("chat-two", outcome.session.id)


def test_invalid_empty_transcription_fails_without_leaking_payload(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    job_id = queue.enqueue_audio(
        "chat-one",
        patient_id,
        filename="recording.ogg",
        payload=b"fictitious audio bytes",
        duration_seconds=10,
        content_type="audio/ogg",
    )
    outcome = asyncio.run(
        ProcessingWorker(queue, records, FakeTranscriber(text="   ")).process_once()
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_payload"
    with database.sessions() as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.payload_enc == b""


def test_retention_expiry_blocks_draft_saving(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    with database.sessions.begin() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        clinical_session.transcript_expires_at = clinical_session.created_at

    with pytest.raises(TranscriptExpired, match="retention period"):
        records.save_draft(
            "chat-one",
            outcome.session.id,
            _draft_json(patient_id, outcome.session.session_date),
        )
    with database.sessions() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        assert clinical_session.transcript_enc == b""


def test_reading_expired_transcript_scrubs_it(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    with database.sessions.begin() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        clinical_session.transcript_expires_at = clinical_session.created_at

    with pytest.raises(TranscriptExpired):
        records.get_transcript("chat-one", outcome.session.id)
    with database.sessions() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        assert clinical_session.transcript_enc == b""


def test_cleanup_purges_expired_transcripts_in_bounded_batches(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    with database.sessions.begin() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        clinical_session.transcript_expires_at = clinical_session.created_at

    assert records.purge_expired_transcripts(limit=1) == 1
    assert records.purge_expired_transcripts(limit=1) == 0
    with database.sessions() as session:
        clinical_session = session.get(ClinicalSession, outcome.session.id)
        assert clinical_session is not None
        assert clinical_session.transcript_enc == b""
        assert clinical_session.status == "DISCARDED"


def test_approved_correction_preserves_previous_version_and_pdf(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    queue.enqueue_text("chat-one", patient_id, TRANSCRIPT)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())
    assert outcome.session is not None
    first_draft = _draft_json(patient_id, outcome.session.session_date)
    first = records.save_draft("chat-one", outcome.session.id, first_draft)
    first_pdf = records.approve("chat-one", outcome.session.id).pdf

    with pytest.raises(DraftRejected, match="reason is required"):
        records.save_draft("chat-one", outcome.session.id, first_draft)
    second = records.save_draft(
        "chat-one",
        outcome.session.id,
        first_draft,
        correction_reason="Correção revisada pelo profissional.",
    )
    assert second.version == 2
    assert records.get_pdf("chat-one", outcome.session.id, version_number=1) == first_pdf
    second_pdf = records.approve("chat-one", outcome.session.id).pdf
    assert records.get_pdf("chat-one", outcome.session.id, version_number=2) == second_pdf

    with database.sessions() as session:
        versions = session.scalars(
            select(SessionVersion)
            .where(SessionVersion.session_id == outcome.session.id)
            .order_by(SessionVersion.version)
        ).all()
        assert [version.version for version in versions] == [1, 2]
        assert versions[0].id == first.version_id
        assert versions[0].approved_at is not None
        assert versions[1].approved_at is not None


def test_worker_returns_idle_and_stops_cleanly(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, _, _ = worker_context
    worker = ProcessingWorker(queue, records, FakeTranscriber(), poll_interval_seconds=0.001)
    assert asyncio.run(worker.process_once()) == WorkerOutcome(status="idle")

    async def stop_after_poll() -> None:
        stop_event = asyncio.Event()
        asyncio.get_running_loop().call_later(0.01, stop_event.set)
        await worker.run(stop_event)

    asyncio.run(stop_after_poll())


def test_transcriber_failure_is_logged_without_retrying_payload_contents(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
) -> None:
    _, queue, records, database, patient_id = worker_context
    job_id = queue.enqueue_audio(
        "chat-one",
        patient_id,
        filename="recording.ogg",
        payload=b"private audio bytes",
        duration_seconds=10,
        content_type="audio/ogg",
    )
    outcome = asyncio.run(
        ProcessingWorker(
            queue,
            records,
            FakeTranscriber(error=RuntimeError("private audio bytes")),
        ).process_once()
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "transcription_failed"
    with database.sessions() as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        assert job.status == "pending"
        assert b"private audio bytes" not in job.payload_enc


def test_revoked_consent_while_saving_fails_job_and_clears_audio(
    worker_context: tuple[IdentityService, JobQueue, ClinicalRecordService, Database, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, queue, records, database, patient_id = worker_context
    job_id = queue.enqueue_audio(
        "chat-one",
        patient_id,
        filename="recording.ogg",
        payload=b"fictitious audio bytes",
        duration_seconds=10,
        content_type="audio/ogg",
    )

    def reject_save(**kwargs: object) -> None:
        del kwargs
        raise ConsentRequired("Consent was revoked during persistence")

    monkeypatch.setattr(records, "create_transcribed_session", reject_save)
    outcome = asyncio.run(ProcessingWorker(queue, records, FakeTranscriber()).process_once())

    assert outcome.error_code == "consent_revoked"
    with database.sessions() as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.payload_enc == b""


def test_worker_main_requires_encryption_and_admin_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import carevoice.jobs.worker as worker_module
    from carevoice.config import Settings

    missing_key = Settings.model_construct(
        field_encryption_key=None,
        administrator_chat_id=None,
        transcriber="local",
    )
    monkeypatch.setattr(worker_module, "Settings", lambda: missing_key)
    with pytest.raises(ValueError, match="CAREVOICE_FIELD_ENCRYPTION_KEY"):
        main()
    missing_admin = Settings.model_construct(
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
        administrator_chat_id=None,
        transcriber="local",
    )
    monkeypatch.setattr(worker_module, "Settings", lambda: missing_admin)
    with pytest.raises(ValueError, match="CAREVOICE_ADMINISTRATOR_CHAT_ID"):
        main()


def test_pdf_renderer_supports_abc_free_unicode_and_html_text() -> None:
    renderer = PDFRenderer()
    identity = PDFIdentity(
        patient_name="Paciente & <Teste>",
        professional_name="Fonoaudióloga",
        profession="Fonoaudióloga",
        council_registry="REG-123",
    )
    payloads = (
        {
            "patient_id": "patient-123",
            "session_date": "2026-11-10",
            "template": "ABC",
            "content": {
                "kind": "ABC",
                "episodes": [
                    {
                        "antecedent": _claim("Foi solicitado sentar.", "solicitado sentar"),
                        "behavior": _claim("Levantou da cadeira.", "levantou da cadeira"),
                        "consequence": _claim(
                            "A atividade foi interrompida.",
                            "atividade foi interrompida",
                        ),
                        "hypothesized_function": _claim("Buscou atenção.", "buscou atenção"),
                    }
                ],
                "measures": [
                    {
                        "value": "80",
                        "unit": "percentage",
                        "is_approximate": True,
                        "original_expression": "quase 80%",
                        "evidence": [{"quote": "quase 80%"}],
                    }
                ],
            },
        },
        {
            "patient_id": "patient-123",
            "session_date": "2026-11-10",
            "template": "FREE",
            "content": {
                "kind": "FREE",
                "sections": [
                    {
                        "title": "Observações & <avaliação>",
                        "claims": [
                            _claim("Descrição com caracteres especiais.", "caracteres especiais")
                        ],
                    }
                ],
            },
        },
    )

    for payload in payloads:
        draft = StructuredDraft.model_validate(payload)
        pdf = renderer.render(draft, identity)
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 1000


def _claim(text: str, quote: str) -> dict[str, object]:
    return {"text": text, "evidence": [{"quote": quote}]}


def _draft_json(patient_id: str, session_date: date) -> str:
    return json.dumps(
        {
            "patient_id": patient_id,
            "session_date": session_date.isoformat(),
            "template": "SOAP",
            "content": {
                "kind": "SOAP",
                "subjective": [
                    {
                        "text": "A mãe relatou melhora.",
                        "evidence": [{"quote": "A mãe relatou melhora"}],
                    }
                ],
                "objective": [
                    {
                        "text": "Descreveu 80% de acerto.",
                        "evidence": [{"quote": "80% de acerto durante a atividade"}],
                    }
                ],
            },
        }
    )
