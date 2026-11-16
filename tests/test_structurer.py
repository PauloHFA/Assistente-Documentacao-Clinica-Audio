import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from carevoice.structure.dates import resolve_session_date
from carevoice.structure.llm import (
    ClinicalStructurer,
    StructuringContext,
    StructuringFailed,
    UrllibLLMTransport,
)
from carevoice.structure.pseudonymizer import (
    filter_internal_parent_summary,
    pseudonymize_transcript,
    restore_draft_pseudonyms,
)
from carevoice.structure.schemas import StructuredDraft


class FakeLLMTransport:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(self, messages: Sequence[dict[str, str]]) -> str:
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


def test_pseudonymizer_masks_and_restores_names_with_accents() -> None:
    transcript = "João Pedro manteve contato visual. A mãe Maria Silva relatou melhora."
    masked = pseudonymize_transcript(
        transcript,
        patient_name="João Pedro",
        patient_alias="JP",
        guardian_names=("Maria Silva",),
    )

    assert "João Pedro" not in masked.text
    assert "Maria Silva" not in masked.text
    assert "[PACIENTE]" in masked.text
    assert "[RESPONSAVEL_1]" in masked.text

    draft = StructuredDraft.model_validate(
        {
            "patient_id": "p-1",
            "session_date": "2026-11-15",
            "template": "SOAP",
            "content": {
                "kind": "SOAP",
                "subjective": [
                    {
                        "text": "[RESPONSAVEL_1] relatou melhora.",
                        "evidence": [{"quote": "A mãe [RESPONSAVEL_1] relatou melhora."}],
                    }
                ],
                "objective": [
                    {
                        "text": "[PACIENTE] manteve contato visual.",
                        "evidence": [{"quote": "[PACIENTE] manteve contato visual."}],
                    }
                ],
                "assessment": [],
                "plan": [],
                "parent_summary": [],
                "measures": [],
            },
        }
    )
    restored = restore_draft_pseudonyms(draft, masked.mapping)
    assert restored.content.kind == "SOAP"
    assert restored.content.objective[0].text == "João Pedro manteve contato visual."
    assert restored.content.subjective[0].text == "Maria Silva relatou melhora."


def test_filter_internal_parent_summary_omits_internal_tag() -> None:
    draft = StructuredDraft.model_validate(
        {
            "patient_id": "p-1",
            "session_date": "2026-11-15",
            "template": "SOAP",
            "content": {
                "kind": "SOAP",
                "subjective": [],
                "objective": [],
                "assessment": [],
                "plan": [],
                "parent_summary": [
                    {
                        "text": "Nota clínica #interno confidencial.",
                        "evidence": [{"quote": "Nota clínica #interno confidencial."}],
                    },
                    {
                        "text": "Boa participação nas atividades.",
                        "evidence": [{"quote": "Boa participação nas atividades."}],
                    },
                ],
                "measures": [],
            },
        }
    )
    filtered = filter_internal_parent_summary(draft)
    assert filtered.content.kind == "SOAP"
    assert len(filtered.content.parent_summary) == 1
    assert filtered.content.parent_summary[0].text == "Boa participação nas atividades."


def test_resolve_session_date_handles_pt_br_expressions() -> None:
    ref = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # Monday
    assert resolve_session_date("Sessão de ontem com avanço.", reference_now=ref) == date(
        2026, 11, 15
    )
    assert resolve_session_date("Atendimento de anteontem.", reference_now=ref) == date(
        2026, 11, 14
    )
    assert resolve_session_date("Sessão de sexta-feira passada.", reference_now=ref) == date(
        2026, 11, 13
    )
    assert resolve_session_date("Sessão do dia 05/11/2026.", reference_now=ref) == date(2026, 11, 5)
    assert resolve_session_date("Sessão regular hoje.", reference_now=ref) == date(2026, 11, 16)


def test_clinical_structurer_pseudonymizes_retries_and_restores() -> None:
    transcript = "Lucas alcançou 80% de acertos. #interno Suspeita de fadiga escolar."
    context = StructuringContext(
        patient_id="pat-1",
        patient_name="Lucas",
        patient_alias=None,
        guardian_names=(),
        session_date=date(2026, 11, 16),
        template="SOAP",
        active_goals=("[g1] Nomeação (percentage)",),
    )

    bad_json = "not-json"
    ungrounded_json = json.dumps(
        {
            "patient_id": "pat-1",
            "session_date": "2026-11-16",
            "template": "SOAP",
            "content": {
                "kind": "SOAP",
                "subjective": [],
                "objective": [
                    {
                        "text": "[PACIENTE] alcançou 99% de acertos.",
                        "evidence": [{"quote": "citação inventada"}],
                    }
                ],
                "assessment": [],
                "plan": [],
                "parent_summary": [],
                "measures": [],
            },
        }
    )
    good_json = json.dumps(
        {
            "patient_id": "pat-1",
            "session_date": "2026-11-16",
            "template": "SOAP",
            "content": {
                "kind": "SOAP",
                "subjective": [],
                "objective": [
                    {
                        "text": "[PACIENTE] alcançou 80% de acertos.",
                        "evidence": [{"quote": "[PACIENTE] alcançou 80% de acertos."}],
                    }
                ],
                "assessment": [],
                "plan": [],
                "parent_summary": [
                    {
                        "text": "#interno Suspeita de fadiga escolar.",
                        "evidence": [{"quote": "#interno Suspeita de fadiga escolar."}],
                    }
                ],
                "measures": [
                    {
                        "value": 80,
                        "unit": "percentage",
                        "is_approximate": False,
                        "original_expression": "80%",
                        "goal_id": "g1",
                        "evidence": [{"quote": "[PACIENTE] alcançou 80% de acertos."}],
                    }
                ],
            },
        }
    )

    transport = FakeLLMTransport([bad_json, ungrounded_json, good_json])
    structurer = ClinicalStructurer(
        transport, pseudonymize_before_llm=True, max_corrective_retries=2
    )
    outcome = asyncio.run(structurer.structure(transcript, context))

    assert outcome.attempts == 3
    assert outcome.pseudonymized is True
    assert outcome.fidelity.grounded is True
    assert outcome.draft.content.kind == "SOAP"
    assert outcome.draft.content.objective[0].text == "Lucas alcançou 80% de acertos."
    assert outcome.draft.content.parent_summary == ()
    # Ensure real name was NOT sent in the initial user prompt
    assert "Lucas" not in transport.calls[0][1]["content"]
    assert "[PACIENTE]" in transport.calls[0][1]["content"]


def test_clinical_structurer_raises_after_exhausting_retries() -> None:
    context = StructuringContext(
        patient_id="pat-1",
        patient_name="Lucas",
        patient_alias=None,
        guardian_names=(),
        session_date=date(2026, 11, 16),
        template="FREE",
    )
    transport = FakeLLMTransport(["{}", "{}"])
    structurer = ClinicalStructurer(transport, max_corrective_retries=1)
    with pytest.raises(StructuringFailed):
        asyncio.run(structurer.structure("Sessão realizada sem intercorrências.", context))


def test_urllib_llm_transport_blocks_cloud_without_opt_in() -> None:
    with pytest.raises(PermissionError, match="CAREVOICE_CLOUD_PROCESSING_ENABLED=true"):
        UrllibLLMTransport(
            provider="cloud",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-test",
            cloud_processing_enabled=False,
        )


def test_urllib_llm_transport_posts_ollama_and_cloud_json() -> None:
    from io import BytesIO
    from unittest.mock import patch

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._buf = BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    ollama = UrllibLLMTransport(
        provider="ollama", base_url="http://localhost:11434", model="qwen2.5:7b"
    )
    with patch(
        "urllib.request.urlopen",
        return_value=_Resp(json.dumps({"message": {"content": "{}"}}).encode()),
    ):
        assert asyncio.run(ollama.complete_json([{"role": "user", "content": "oi"}])) == "{}"

    cloud = UrllibLLMTransport(
        provider="cloud",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key="sk-test",
        cloud_processing_enabled=True,
    )
    with patch(
        "urllib.request.urlopen",
        return_value=_Resp(
            json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        ),
    ):
        assert (
            asyncio.run(cloud.complete_json([{"role": "user", "content": "oi"}])) == '{"ok": true}'
        )


def test_abc_and_free_pseudonym_restoration() -> None:
    abc_draft = StructuredDraft.model_validate(
        {
            "patient_id": "p-1",
            "session_date": "2026-11-16",
            "template": "ABC",
            "content": {
                "kind": "ABC",
                "episodes": [
                    {
                        "antecedent": {
                            "text": "[PACIENTE] recebeu demanda.",
                            "evidence": [{"quote": "[PACIENTE] recebeu demanda."}],
                        },
                        "behavior": {
                            "text": "[PACIENTE] pediu pausa.",
                            "evidence": [{"quote": "[PACIENTE] pediu pausa."}],
                        },
                        "consequence": {
                            "text": "[RESPONSAVEL_1] validou.",
                            "evidence": [{"quote": "[RESPONSAVEL_1] validou."}],
                        },
                        "hypothesized_function": {
                            "text": "Comunicação funcional de [PACIENTE].",
                            "evidence": [{"quote": "[PACIENTE] pediu pausa."}],
                        },
                    }
                ],
                "parent_summary": [],
                "measures": [],
            },
        }
    )
    masked = pseudonymize_transcript(
        "Lucas pediu pausa quando Ana validou.",
        patient_name="Lucas",
        guardian_names=("Ana",),
    )
    restored = restore_draft_pseudonyms(abc_draft, masked.mapping)
    assert restored.content.kind == "ABC"
    assert restored.content.episodes[0].antecedent.text == "Lucas recebeu demanda."
    assert restored.content.episodes[0].consequence.text == "Ana validou."

    free_draft = StructuredDraft.model_validate(
        {
            "patient_id": "p-1",
            "session_date": "2026-11-16",
            "template": "FREE",
            "content": {
                "kind": "FREE",
                "sections": [
                    {
                        "title": "Evolução de [PACIENTE]",
                        "claims": [
                            {
                                "text": "[PACIENTE] participou.",
                                "evidence": [{"quote": "[PACIENTE] participou."}],
                            }
                        ],
                    }
                ],
                "parent_summary": [],
                "measures": [],
            },
        }
    )
    restored_free = restore_draft_pseudonyms(free_draft, masked.mapping)
    assert restored_free.content.kind == "FREE"
    assert restored_free.content.sections[0].title == "Evolução de Lucas"

    # Also exercise ClinicalStructurer with ABC template and metadata-mismatch retry
    abc_context = StructuringContext(
        patient_id="p-1",
        patient_name="Lucas",
        patient_alias=None,
        guardian_names=("Ana",),
        session_date=date(2026, 11, 16),
        template="ABC",
    )
    mismatch = json.loads(abc_draft.model_dump_json())
    mismatch["patient_id"] = "wrong-patient"
    transport = FakeLLMTransport([json.dumps(mismatch), abc_draft.model_dump_json()])
    abc_outcome = asyncio.run(
        ClinicalStructurer(transport).structure(
            "Lucas recebeu demanda. Lucas pediu pausa. Ana validou.",
            abc_context,
        )
    )
    assert abc_outcome.attempts == 2
    assert abc_outcome.draft.content.kind == "ABC"


def test_worker_auto_structures_draft_when_structurer_is_configured() -> None:
    from cryptography.fernet import Fernet

    from carevoice.db.database import Database
    from carevoice.domain.identity import IdentityService
    from carevoice.domain.records import ClinicalRecordService
    from carevoice.jobs.queue import JobQueue
    from carevoice.jobs.worker import ProcessingWorker
    from carevoice.security.crypto import FieldCipher
    from carevoice.transcribe.base import AudioFormat, TranscriptionResult

    class DummyTranscriber:
        async def transcribe(
            self,
            payload: bytes,
            audio_format: AudioFormat,
            glossary_terms: Sequence[str] = (),
        ) -> TranscriptionResult:
            del payload, audio_format, glossary_terms
            return TranscriptionResult(text="unused", language="pt")

    db = Database("sqlite:///:memory:")
    db.create_schema_for_tests()
    try:
        cipher = FieldCipher(Fernet.generate_key().decode("ascii"))
        identity = IdentityService(db.sessions, cipher, "admin-chat")
        code = identity.create_invitation(
            "admin-chat",
            name="Dr. Paulo",
            profession="Fono",
            council_registry="CRFa-1",
        )
        identity.consume_invitation(code, "pro-chat")
        patient = identity.create_patient(
            "pro-chat",
            display_name="Ana Clara",
            guardian_name="Carla",
            guardian_relationship="Mãe",
        )
        identity.grant_consent("pro-chat", patient.id, terms_version="v1", purpose="clínica")
        queue = JobQueue(db.sessions, identity, cipher)
        records = ClinicalRecordService(db.sessions, cipher)
        transcript = "Sessão de ontem: Ana Clara manteve foco por 15 minutos."
        queue.enqueue_text("pro-chat", patient.id, transcript)

        expected_date = resolve_session_date(transcript).isoformat()
        payload = json.dumps(
            {
                "patient_id": patient.id,
                "session_date": expected_date,
                "template": "SOAP",
                "content": {
                    "kind": "SOAP",
                    "subjective": [],
                    "objective": [
                        {
                            "text": "[PACIENTE] manteve foco por 15 minutos.",
                            "evidence": [{"quote": "[PACIENTE] manteve foco por 15 minutos."}],
                        }
                    ],
                    "assessment": [],
                    "plan": [],
                    "parent_summary": [],
                    "measures": [],
                },
            }
        )
        worker = ProcessingWorker(
            queue,
            records,
            DummyTranscriber(),
            structurer=ClinicalStructurer(FakeLLMTransport([payload])),
            database=db,
            cipher=cipher,
        )
        outcome = asyncio.run(worker.process_once())
        assert outcome.status == "completed"
        assert outcome.session is not None
        draft = records.get_session_draft("pro-chat", outcome.session.id)
        assert draft.content.kind == "SOAP"
        assert draft.content.objective[0].text == "Ana Clara manteve foco por 15 minutos."
    finally:
        db.dispose()
