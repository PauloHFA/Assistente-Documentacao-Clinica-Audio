import json

from cryptography.fernet import Fernet

from carevoice.bot.router import CareVoiceBotHandler
from carevoice.db.database import Database
from carevoice.domain.identity import IdentityService
from carevoice.domain.records import ClinicalRecordService
from carevoice.jobs.queue import JobQueue
from carevoice.security.crypto import FieldCipher


def test_bot_end_to_end_commands_and_review_callbacks() -> None:
    db = Database("sqlite:///:memory:")
    db.create_schema_for_tests()
    try:
        cipher = FieldCipher(Fernet.generate_key().decode("ascii"))
        identity = IdentityService(db.sessions, cipher, "admin-1")
        queue = JobQueue(db.sessions, identity, cipher)
        records = ClinicalRecordService(db.sessions, cipher)
        bot = CareVoiceBotHandler(identity, queue, records)

        # Unauthenticated status fails safely
        assert "Acesso não autenticado" in bot.handle_text("pro-1", "/status").text

        # Admin issues invite via /convite and professional joins via /entrar
        invite_resp = bot.handle_text("admin-1", "/convite Dra. Julia | Fono | CRFa-99")
        code = invite_resp.text.split(": ", 1)[1]
        join_resp = bot.handle_text("pro-1", f"/entrar {code}")
        assert "Conta vinculada" in join_resp.text

        # Create patient with alias so "Lucas" is genuinely ambiguous
        p1_resp = bot.handle_text(
            "pro-1", "/paciente novo Lucas Silva | apelido=Lucas | template=SOAP"
        )
        p1_id = p1_resp.text.split("id: ", 1)[1].split(",", 1)[0]
        p2_resp = bot.handle_text("pro-1", "/paciente novo Lucas | template=FREE")
        p2_id = p2_resp.text.split("id: ", 1)[1].split(",", 1)[0]
        assert "Lucas Silva" in bot.handle_text("pro-1", "/pacientes").text
        assert "Sessão ativa" in bot.handle_text("pro-1", "/status").text

        consent_resp = bot.handle_text("pro-1", f"/consentimento {p1_id} | versao=v1")
        assert "Consentimento registrado" in consent_resp.text

        goal_resp = bot.handle_text(
            "pro-1",
            f"/meta nova {p1_id} | Contato visual | Comunicação | percentage | 80%",
        )
        assert "Meta criada" in goal_resp.text
        assert "Contato visual" in bot.handle_text("pro-1", f"/metas {p1_id}").text

        # Ambiguous patient triggers inline buttons then pick_patient callback
        ambig = bot.handle_text(
            "pro-1",
            "Lucas: Sessão com bom engajamento e resposta imediata às solicitações.",
        )
        assert ambig.buttons
        picked = bot.handle_callback("pro-1", f"pick_patient:{p1_id}")
        assert "enfileirado" in picked.text

        # Process job -> save draft -> /sessao -> approve callback -> PDF attachment
        claimed = queue.claim_next()
        assert claimed is not None
        transcript = "Sessão com bom engajamento e resposta imediata às solicitações."
        session = records.create_transcribed_session(
            job_id=claimed.id,
            professional_id=claimed.professional_id,
            patient_id=p1_id,
            transcript=transcript,
        )
        queue.complete(claimed.id)
        records.save_draft(
            "pro-1",
            session.id,
            json.dumps(
                {
                    "patient_id": p1_id,
                    "session_date": session.session_date.isoformat(),
                    "template": "SOAP",
                    "content": {
                        "kind": "SOAP",
                        "subjective": [],
                        "objective": [{"text": transcript, "evidence": [{"quote": transcript}]}],
                        "assessment": [],
                        "plan": [],
                        "parent_summary": [],
                        "measures": [],
                    },
                }
            ),
        )

        sess_view = bot.handle_text("pro-1", f"/sessao {session.id}")
        assert sess_view.buttons
        approved_resp = bot.handle_callback("pro-1", f"approve:{session.id}")
        assert approved_resp.document is not None
        assert approved_resp.document.content.startswith(b"%PDF-")

        assert "Histórico aprovado" in bot.handle_text("pro-1", f"/historico {p1_id}").text
        assert "engajamento" in bot.handle_text("pro-1", "/buscar engajamento").text
        assert "Contato visual" in bot.handle_text("pro-1", f"/progresso {p1_id}").text
        assert "auditoria" in bot.handle_text("pro-1", "/auditoria").text.casefold()
        assert bot.handle_text("pro-1", f"/exportar {p1_id}").document is not None
        day_str = session.session_date.isoformat()
        rep = bot.handle_text("pro-1", f"/relatorio {p1_id} {day_str} {day_str}")
        assert rep.document is not None
        assert "piloto" in bot.handle_text("pro-1", "/privacidade").text
        assert "CareVoice AI" in bot.handle_text("pro-1", "/ajuda").text
        assert "Comando não reconhecido" in bot.handle_text("pro-1", "/desconhecido").text

        # Audio handling + ambiguity + edit/redo/discard + consent revocation
        audio_ambig = bot.handle_audio(
            "pro-1",
            patient_hint="Lucas",
            filename="nota.ogg",
            payload=b"ogg-bytes",
            duration_seconds=12.0,
            content_type="audio/ogg",
        )
        assert audio_ambig.buttons
        audio_picked = bot.handle_callback("pro-1", f"pick_patient:{p1_id}")
        assert "enfileirado" in audio_picked.text

        # Test edit / redo / discard on a second session for p2
        bot.handle_text("pro-1", f"/consentimento {p2_id} | versao=v2 | finalidade=teste")
        revoked = bot.handle_text("pro-1", f"/revogar_consentimento {p2_id}")
        assert "revogado" in revoked.text.casefold()
        regranted = bot.handle_text("pro-1", f"/consentimento {p2_id}")
        assert "Consentimento registrado" in regranted.text

        # Check command usage / validation messages
        assert "Uso:" in bot.handle_text("pro-1", "/entrar").text
        assert "Uso:" in bot.handle_text("admin-1", "/convite incompleto").text
        assert "Uso:" in bot.handle_text("pro-1", "/paciente invalido").text
        assert "Uso:" in bot.handle_text("pro-1", "/consentimento").text
        assert "Uso:" in bot.handle_text("pro-1", "/revogar_consentimento").text
        assert "Uso:" in bot.handle_text("pro-1", "/metas").text
        assert "Uso:" in bot.handle_text("pro-1", "/meta invalida").text
        assert "Uso:" in bot.handle_text("pro-1", "/historico").text
        assert "Uso:" in bot.handle_text("pro-1", "/sessao").text
        assert "Uso:" in bot.handle_text("pro-1", "/buscar").text
        assert "Uso:" in bot.handle_text("pro-1", "/progresso").text
        assert "Uso:" in bot.handle_text("pro-1", "/relatorio").text
        assert "Uso:" in bot.handle_text("pro-1", "/exportar").text
        assert "Informe o paciente" in bot.handle_text("pro-1", "texto sem dois pontos").text
        assert "não encontrado" in bot.handle_text("pro-1", "Desconhecido: relato").text

        t_free = "Relato extenso de teste clínico para rascunho."
        q_job = queue.enqueue_text("pro-1", p2_id, t_free)
        # Drain earlier audio job first so q_job is claimed
        audio_claimed = queue.claim_next()
        assert audio_claimed is not None
        queue.complete(audio_claimed.id)
        claimed2 = queue.claim_next()
        assert claimed2 is not None
        assert claimed2.id == q_job
        s2 = records.create_transcribed_session(
            job_id=q_job,
            professional_id=claimed2.professional_id,
            patient_id=p2_id,
            transcript=t_free,
        )
        queue.complete(q_job)
        assert "Para refazer" in bot.handle_callback("pro-1", f"redo:{s2.id}").text
        assert "Envie o JSON" in bot.handle_callback("pro-1", f"edit:{s2.id}").text
        edited = bot.handle_text(
            "pro-1",
            json.dumps(
                {
                    "patient_id": p2_id,
                    "session_date": s2.session_date.isoformat(),
                    "template": "FREE",
                    "content": {
                        "kind": "FREE",
                        "sections": [
                            {
                                "title": "Observação",
                                "claims": [
                                    {
                                        "text": t_free,
                                        "evidence": [{"quote": t_free}],
                                    }
                                ],
                            }
                        ],
                        "parent_summary": [],
                        "measures": [],
                    },
                }
            ),
        )
        assert "Versão v1 salva" in edited.text
        assert "descartada" in bot.handle_callback("pro-1", f"discard:{s2.id}").text

        # Test editing an approved session with motivo=... | <json>
        assert "Envie o JSON" in bot.handle_callback("pro-1", f"edit:{session.id}").text
        rectified = bot.handle_text(
            "pro-1",
            "motivo=Ajuste clínico | "
            + json.dumps(
                {
                    "patient_id": p1_id,
                    "session_date": session.session_date.isoformat(),
                    "template": "SOAP",
                    "content": {
                        "kind": "SOAP",
                        "subjective": [],
                        "objective": [{"text": transcript, "evidence": [{"quote": transcript}]}],
                        "assessment": [],
                        "plan": [],
                        "parent_summary": [],
                        "measures": [],
                    },
                }
            ),
        )
        assert "Versão v2 salva" in rectified.text
    finally:
        db.dispose()
