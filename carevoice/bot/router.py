"""Command and clinical-workflow router for the Telegram bot adapter."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

from carevoice.domain.identity import (
    AuthenticationError,
    AuthorizationError,
    ConsentRequired,
    IdentityService,
    InvitationError,
    PatientTemplate,
    ResourceNotFound,
)
from carevoice.domain.records import ClinicalRecordService, DraftRejected
from carevoice.jobs.queue import DailyQuotaExceeded, JobQueue
from carevoice.structure.patient_match import resolve_patient_candidate
from carevoice.structure.schemas import Template


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class DocumentAttachment:
    filename: str
    content: bytes
    caption: str = ""


@dataclass(frozen=True)
class BotResponse:
    text: str
    buttons: tuple[tuple[InlineButton, ...], ...] = ()
    document: DocumentAttachment | None = None


PendingMode = Literal["pick_patient_text", "pick_patient_audio", "edit_draft"]


@dataclass
class _PendingInteraction:
    mode: PendingMode
    text_payload: str = ""
    audio_filename: str = ""
    audio_payload: bytes = b""
    audio_duration: float = 0.0
    audio_content_type: str | None = None
    session_id: str = ""


class CareVoiceBotHandler:
    """Stateless-friendly handler over IdentityService, JobQueue, and ClinicalRecordService."""

    def __init__(
        self,
        identity: IdentityService,
        queue: JobQueue,
        records: ClinicalRecordService,
        *,
        real_data_enabled: bool = False,
    ) -> None:
        self._identity = identity
        self._queue = queue
        self._records = records
        self._real_data_enabled = real_data_enabled
        self._pending: dict[str, _PendingInteraction] = {}

    def handle_text(self, chat_id: str, raw_text: str) -> BotResponse:
        text = raw_text.strip()
        if not text:
            return BotResponse(text="Envie um comando, áudio ou relato clínico.")

        if text.startswith("/"):
            return self._dispatch_command(chat_id, text)

        pending = self._pending.pop(chat_id, None)
        if pending is not None and pending.mode == "edit_draft":
            return self._apply_draft_edit(chat_id, pending.session_id, text)

        return self._enqueue_clinical_text(chat_id, text)

    def handle_audio(
        self,
        chat_id: str,
        *,
        patient_hint: str,
        filename: str,
        payload: bytes,
        duration_seconds: float,
        content_type: str | None = None,
    ) -> BotResponse:
        try:
            patients = self._identity.list_patients(chat_id)
        except AuthenticationError:
            return BotResponse(
                text="Acesso não vinculado. Use /entrar <codigo> com um convite válido."
            )
        resolution = resolve_patient_candidate(patient_hint, patients)
        if resolution.status == "not_found":
            return BotResponse(
                text=(
                    "Paciente não encontrado. Informe o nome ou apelido na legenda do áudio "
                    "ou liste seus pacientes com /pacientes."
                )
            )
        if resolution.status == "ambiguous":
            self._pending[chat_id] = _PendingInteraction(
                mode="pick_patient_audio",
                audio_filename=filename,
                audio_payload=payload,
                audio_duration=duration_seconds,
                audio_content_type=content_type,
            )
            buttons = tuple(
                (InlineButton(text=p.display_name, callback_data=f"pick_patient:{p.id}"),)
                for p in resolution.candidates
            )
            return BotResponse(
                text="Mais de um paciente corresponde ao nome. Selecione abaixo:",
                buttons=buttons,
            )
        assert resolution.patient is not None
        return self._enqueue_audio_for_patient(
            chat_id,
            resolution.patient.id,
            resolution.patient.display_name,
            filename=filename,
            payload=payload,
            duration_seconds=duration_seconds,
            content_type=content_type,
        )

    def handle_callback(self, chat_id: str, callback_data: str) -> BotResponse:
        action, _, target_id = callback_data.partition(":")
        if not target_id:
            return BotResponse(text="Ação inválida.")

        if action == "pick_patient":
            pending = self._pending.pop(chat_id, None)
            if pending is None:
                return BotResponse(text="Nenhuma seleção pendente encontrada.")
            if pending.mode == "pick_patient_text":
                return self._enqueue_text_for_patient(chat_id, target_id, pending.text_payload)
            if pending.mode == "pick_patient_audio":
                return self._enqueue_audio_for_patient(
                    chat_id,
                    target_id,
                    target_id,
                    filename=pending.audio_filename,
                    payload=pending.audio_payload,
                    duration_seconds=pending.audio_duration,
                    content_type=pending.audio_content_type,
                )

        if action == "approve":
            try:
                approved = self._records.approve(chat_id, target_id)
            except (AuthenticationError, ResourceNotFound, DraftRejected) as exc:
                return BotResponse(text=f"Não foi possível aprovar: {exc}")
            return BotResponse(
                text=f"Evolução aprovada (sessão {approved.session_id}). PDF gerado abaixo.",
                document=DocumentAttachment(
                    filename=f"evolucao_{approved.session_id[:8]}.pdf",
                    content=approved.pdf,
                    caption="Documento elaborado com apoio de IA e revisado pelo profissional.",
                ),
            )

        if action == "discard":
            try:
                self._records.discard_session(chat_id, target_id)
            except (AuthenticationError, ResourceNotFound, DraftRejected) as exc:
                return BotResponse(text=f"Não foi possível descartar: {exc}")
            return BotResponse(text=f"Sessão {target_id} descartada.")

        if action == "edit":
            self._pending[chat_id] = _PendingInteraction(mode="edit_draft", session_id=target_id)
            return BotResponse(
                text=(
                    f"Envie o JSON atualizado para a sessão {target_id} "
                    "(para sessões já aprovadas, informe `motivo=<justificativa> | <json>`)."
                )
            )

        if action == "redo":
            return BotResponse(
                text=(
                    f"Para refazer a sessão {target_id}, envie novamente o relato ou áudio "
                    "no formato `<Nome do Paciente>: <relato>`."
                )
            )

        return BotResponse(text="Ação não reconhecida.")

    def _dispatch_command(self, chat_id: str, text: str) -> BotResponse:
        command_part, _, rest = text.partition(" ")
        command = command_part.split("@", 1)[0].casefold()
        arg = rest.strip()

        if command in {"/start", "/ajuda"}:
            return BotResponse(
                text=(
                    "CareVoice AI — Assistente de Documentação Clínica\n\n"
                    "Comandos disponíveis:\n"
                    "/entrar <codigo> — vincular conta via convite\n"
                    "/paciente novo <nome> | apelido=... | template=SOAP|ABC|FREE\n"
                    "/pacientes — listar pacientes ativos\n"
                    "/consentimento <paciente_id> | versao=v1 | finalidade=...\n"
                    "/metas <paciente_id> — listar metas\n"
                    "/meta nova <paciente_id> | <desc> | <area> | <tipo> | <criterio>\n"
                    "/sessao <session_id> — revisar e aprovar rascunho\n"
                    "/historico <paciente_id> — evoluções aprovadas\n"
                    "/buscar <termo> — buscar nas sessões\n"
                    "/progresso <paciente_id> — tendência das metas\n"
                    "/relatorio <paciente_id> <YYYY-MM-DD> <YYYY-MM-DD> — relatório PDF\n"
                    "/exportar <paciente_id> — exportar ZIP do paciente\n"
                    "/auditoria — últimos eventos de auditoria\n"
                    "/status | /privacidade\n\n"
                    "Para registrar evolução por texto, envie:\n"
                    "`Nome do Paciente: relato da sessão...`"
                )
            )

        if command == "/privacidade":
            mode = (
                "dados reais liberados" if self._real_data_enabled else "piloto com dados fictícios"
            )
            return BotResponse(
                text=(
                    f"Modo atual: {mode}.\n"
                    "Transcrição e estruturação preservam isolamento por profissional, "
                    "criptografia em repouso e validação factual obrigatória antes da aprovação."
                )
            )

        if command == "/entrar":
            if not arg:
                return BotResponse(text="Uso: /entrar <codigo_convite>")
            try:
                professional_id = self._identity.consume_invitation(arg, chat_id)
            except InvitationError as exc:
                return BotResponse(text=f"Convite inválido: {exc}")
            return BotResponse(text=f"Conta vinculada com sucesso (id: {professional_id}).")

        if command == "/convite":
            parts = [p.strip() for p in arg.split("|")]
            if len(parts) < 3:
                return BotResponse(text="Uso: /convite <nome> | <profissao> | <registro>")
            try:
                code = self._identity.create_invitation(
                    chat_id,
                    name=parts[0],
                    profession=parts[1],
                    council_registry=parts[2],
                )
            except (AuthorizationError, ValueError) as exc:
                return BotResponse(text=f"Não autorizado: {exc}")
            return BotResponse(text=f"Convite gerado (uso único): {code}")

        try:
            return self._dispatch_authenticated(chat_id, command, arg)
        except AuthenticationError:
            return BotResponse(
                text="Acesso não autenticado. Use /entrar <codigo> para vincular seu chat."
            )
        except (
            ResourceNotFound,
            ConsentRequired,
            DraftRejected,
            DailyQuotaExceeded,
            ValueError,
        ) as exc:
            return BotResponse(text=f"Erro: {exc}")

    def _dispatch_authenticated(self, chat_id: str, command: str, arg: str) -> BotResponse:
        if command == "/status":
            patients = self._identity.list_patients(chat_id)
            return BotResponse(text=f"Sessão ativa. Pacientes cadastrados: {len(patients)}.")

        if command == "/pacientes":
            patients = self._identity.list_patients(chat_id)
            if not patients:
                return BotResponse(text="Nenhum paciente cadastrado. Use /paciente novo <nome>.")
            lines = [f"- {p.display_name} (id: {p.id}, template: {p.template})" for p in patients]
            return BotResponse(text="Pacientes ativos:\n" + "\n".join(lines))

        if command == "/paciente":
            sub, _, payload = arg.partition(" ")
            if sub.casefold() != "novo" or not payload.strip():
                return BotResponse(
                    text="Uso: /paciente novo <nome> [| apelido=...] [| template=SOAP|ABC|FREE]"
                )
            parts = [item.strip() for item in payload.split("|")]
            name = parts[0]
            alias: str | None = None
            template: PatientTemplate = "SOAP"
            for extra in parts[1:]:
                k, _, v = extra.partition("=")
                key = k.strip().casefold()
                val = v.strip()
                if key == "apelido" and val:
                    alias = val
                elif key == "template" and val.upper() in {"SOAP", "ABC", "FREE"}:
                    match val.upper():
                        case "SOAP" | "ABC" | "FREE":
                            template = val.upper()  # type: ignore[assignment]
            patient = self._identity.create_patient(
                chat_id,
                display_name=name,
                alias=alias,
                template=template,
            )
            return BotResponse(
                text=(
                    f"Paciente cadastrado: {patient.display_name} "
                    f"(id: {patient.id}, template: {patient.template})."
                )
            )

        if command == "/consentimento":
            parts = [p.strip() for p in arg.split("|") if p.strip()]
            if not parts:
                return BotResponse(
                    text="Uso: /consentimento <paciente_id> [| versao=v1] [| finalidade=...]"
                )
            patient_id = parts[0]
            terms_version = "v1"
            purpose = "documentação clínica assistida"
            for extra in parts[1:]:
                k, _, v = extra.partition("=")
                if k.strip().casefold() == "versao" and v.strip():
                    terms_version = v.strip()
                elif k.strip().casefold() == "finalidade" and v.strip():
                    purpose = v.strip()
            consent_id = self._identity.grant_consent(
                chat_id,
                patient_id,
                terms_version=terms_version,
                purpose=purpose,
            )
            return BotResponse(text=f"Consentimento registrado (id: {consent_id}).")

        if command == "/revogar_consentimento":
            if not arg:
                return BotResponse(text="Uso: /revogar_consentimento <paciente_id>")
            self._identity.revoke_consent(chat_id, arg)
            return BotResponse(text="Consentimento revogado imediatamente.")

        if command == "/metas":
            if not arg:
                return BotResponse(text="Uso: /metas <paciente_id>")
            goals = self._identity.list_goals(chat_id, arg)
            if not goals:
                return BotResponse(text="Nenhuma meta cadastrada para este paciente.")
            lines = [
                (
                    f"- {g.description} [{g.indicator_type}] — "
                    f"critério: {g.mastery_criterion} (id: {g.id})"
                )
                for g in goals
            ]
            return BotResponse(text="Metas:\n" + "\n".join(lines))

        if command == "/meta":
            sub, _, payload = arg.partition(" ")
            parts = [p.strip() for p in payload.split("|")]
            if sub.casefold() != "nova" or len(parts) < 5:
                return BotResponse(
                    text=(
                        "Uso: /meta nova <paciente_id> | <descricao> | <area> | "
                        "<percentage|duration|count|scale> | <criterio>"
                    )
                )
            indicator = parts[3].casefold()
            if indicator not in {"percentage", "duration", "count", "scale"}:
                raise ValueError("Tipo de indicador inválido")
            goal = self._identity.create_goal(
                chat_id,
                parts[0],
                description=parts[1],
                area=parts[2],
                indicator_type=indicator,  # type: ignore[arg-type]
                mastery_criterion=parts[4],
            )
            return BotResponse(text=f"Meta criada: {goal.description} (id: {goal.id}).")

        if command == "/historico":
            if not arg:
                return BotResponse(text="Uso: /historico <paciente_id>")
            items = self._records.history(chat_id, arg)
            if not items:
                return BotResponse(text="Nenhuma evolução aprovada para este paciente.")
            lines = [
                (
                    f"- {s.session_date:%d/%m/%Y} | v{s.version} | "
                    f"{s.template} | {s.title} (sessão: {s.id})"
                )
                for s in items
            ]
            return BotResponse(text="Histórico aprovado:\n" + "\n".join(lines))

        if command == "/sessao":
            if not arg:
                return BotResponse(text="Uso: /sessao <session_id>")
            draft = self._records.get_session_draft(chat_id, arg)
            return BotResponse(
                text=(
                    f"Sessão {arg} ({draft.session_date:%d/%m/%Y}, template {draft.template}):\n"
                    f"Resumo para responsáveis: {len(draft.content.parent_summary)} item(ns)\n"
                    f"Medidas extraídas: {len(draft.content.measures)}"
                ),
                buttons=(
                    (
                        InlineButton(text="✅ Aprovar", callback_data=f"approve:{arg}"),
                        InlineButton(text="✏️ Editar", callback_data=f"edit:{arg}"),
                    ),
                    (
                        InlineButton(text="🔄 Refazer", callback_data=f"redo:{arg}"),
                        InlineButton(text="🗑️ Descartar", callback_data=f"discard:{arg}"),
                    ),
                ),
            )

        if command == "/buscar":
            if not arg:
                return BotResponse(text="Uso: /buscar <termo>")
            matches = self._records.search_sessions(chat_id, arg)
            if not matches:
                return BotResponse(text="Nenhuma sessão encontrada para o termo informado.")
            lines = [
                f"- {m.session_date:%d/%m/%Y} [{m.status}] (sessão: {m.session_id}): {m.snippet}"
                for m in matches
            ]
            return BotResponse(text="Resultados da busca:\n" + "\n".join(lines))

        if command == "/progresso":
            if not arg:
                return BotResponse(text="Uso: /progresso <paciente_id>")
            series = self._records.goal_progress(chat_id, arg)
            if not series:
                return BotResponse(text="Nenhuma meta encontrada para este paciente.")
            lines = [f"- {s.description}: {s.trend_summary}" for s in series]
            return BotResponse(text="Progresso das metas:\n" + "\n".join(lines))

        if command == "/relatorio":
            tokens = arg.split()
            if len(tokens) != 3:
                return BotResponse(text="Uso: /relatorio <paciente_id> <YYYY-MM-DD> <YYYY-MM-DD>")
            start_d = date.fromisoformat(tokens[1])
            end_d = date.fromisoformat(tokens[2])
            pdf = self._records.build_periodic_report_pdf(
                chat_id, tokens[0], start_date=start_d, end_date=end_d
            )
            return BotResponse(
                text=f"Relatório periódico ({start_d:%d/%m/%Y} a {end_d:%d/%m/%Y}) gerado.",
                document=DocumentAttachment(
                    filename=f"relatorio_{tokens[0][:8]}_{start_d}_{end_d}.pdf",
                    content=pdf,
                ),
            )

        if command == "/exportar":
            if not arg:
                return BotResponse(text="Uso: /exportar <paciente_id>")
            bundle = self._records.export_patient_bundle(chat_id, arg)
            return BotResponse(
                text="Exportação do prontuário gerada em arquivo ZIP.",
                document=DocumentAttachment(
                    filename=f"export_{arg[:8]}.zip",
                    content=bundle,
                ),
            )

        if command == "/auditoria":
            events = self._records.list_audit_events(chat_id, limit=20)
            if not events:
                return BotResponse(text="Nenhum evento de auditoria registrado.")
            lines = [
                (
                    f"- {ev.created_at:%d/%m/%Y %H:%M} | {ev.action} "
                    f"({ev.object_type}:{ev.object_id[:8]})"
                )
                for ev in events
            ]
            return BotResponse(text="Últimos eventos de auditoria:\n" + "\n".join(lines))

        return BotResponse(text="Comando não reconhecido. Use /ajuda para ver os comandos.")

    def _enqueue_clinical_text(self, chat_id: str, text: str) -> BotResponse:
        try:
            patients = self._identity.list_patients(chat_id)
        except AuthenticationError:
            return BotResponse(
                text="Acesso não vinculado. Use /entrar <codigo> com um convite válido."
            )
        if ":" not in text:
            return BotResponse(
                text=(
                    "Informe o paciente no início da mensagem: "
                    "`Nome do Paciente: relato da sessão...`"
                )
            )
        spoken_name, _, note = text.partition(":")
        resolution = resolve_patient_candidate(spoken_name.strip(), patients)
        if resolution.status == "not_found":
            return BotResponse(
                text=(
                    "Paciente não encontrado. Verifique com /pacientes "
                    "ou cadastre via /paciente novo."
                )
            )
        if resolution.status == "ambiguous":
            self._pending[chat_id] = _PendingInteraction(
                mode="pick_patient_text",
                text_payload=note.strip(),
            )
            buttons = tuple(
                (InlineButton(text=p.display_name, callback_data=f"pick_patient:{p.id}"),)
                for p in resolution.candidates
            )
            return BotResponse(
                text="Mais de um paciente corresponde ao nome. Selecione abaixo:",
                buttons=buttons,
            )
        assert resolution.patient is not None
        return self._enqueue_text_for_patient(chat_id, resolution.patient.id, note.strip())

    def _enqueue_text_for_patient(self, chat_id: str, patient_id: str, note: str) -> BotResponse:
        try:
            job_id = self._queue.enqueue_text(chat_id, patient_id, note)
        except (ConsentRequired, DailyQuotaExceeded, ResourceNotFound, ValueError) as exc:
            return BotResponse(text=f"Não foi possível enfileirar: {exc}")
        return BotResponse(text=f"Relato recebido e enfileirado (job: {job_id}).")

    def _enqueue_audio_for_patient(
        self,
        chat_id: str,
        patient_id: str,
        patient_label: str,
        *,
        filename: str,
        payload: bytes,
        duration_seconds: float,
        content_type: str | None,
    ) -> BotResponse:
        try:
            job_id = self._queue.enqueue_audio(
                chat_id,
                patient_id,
                filename=filename,
                payload=payload,
                duration_seconds=duration_seconds,
                content_type=content_type,
            )
        except (ConsentRequired, DailyQuotaExceeded, ResourceNotFound, ValueError) as exc:
            return BotResponse(text=f"Não foi possível enfileirar áudio: {exc}")
        return BotResponse(
            text=f"Áudio para {patient_label} enfileirado para processamento (job: {job_id})."
        )

    def _apply_draft_edit(self, chat_id: str, session_id: str, payload: str) -> BotResponse:
        correction_reason: str | None = None
        json_text = payload
        if payload.casefold().startswith("motivo="):
            reason_part, _, rest = payload.partition("|")
            correction_reason = reason_part.split("=", 1)[1].strip() or None
            json_text = rest.strip()
        try:
            json.loads(json_text)
            saved = self._records.save_draft(
                chat_id,
                session_id,
                json_text,
                correction_reason=correction_reason,
            )
        except Exception as exc:
            return BotResponse(text=f"Falha ao atualizar rascunho: {exc}")
        status_note = (
            "Fidelidade validada."
            if saved.fidelity.grounded
            else f"Pendências de fidelidade: {len(saved.fidelity.issues)}."
        )
        return BotResponse(
            text=f"Versão v{saved.version} salva para sessão {saved.session_id}. {status_note}",
            buttons=(
                (
                    InlineButton(text="✅ Aprovar", callback_data=f"approve:{saved.session_id}"),
                    InlineButton(text="✏️ Editar", callback_data=f"edit:{saved.session_id}"),
                ),
            ),
        )


def supported_templates() -> tuple[Template, ...]:
    return ("SOAP", "ABC", "FREE")
