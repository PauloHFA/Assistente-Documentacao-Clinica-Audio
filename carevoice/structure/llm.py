"""Provider-independent clinical draft structuring with pseudonymization and corrective retries."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from carevoice.fidelity.verifier import FidelityReport, verify_draft_fidelity
from carevoice.structure.pseudonymizer import (
    filter_internal_parent_summary,
    pseudonymize_transcript,
    restore_draft_pseudonyms,
)
from carevoice.structure.schemas import StructuredDraft, Template

LLMProvider = Literal["ollama", "cloud"]


class StructuringFailed(Exception):
    """Raised when the LLM cannot produce a valid, grounded draft after retries."""


class LLMTransport(Protocol):
    async def complete_json(self, messages: Sequence[dict[str, str]]) -> str: ...


@dataclass(frozen=True)
class StructuringContext:
    patient_id: str
    patient_name: str
    patient_alias: str | None
    guardian_names: tuple[str, ...]
    session_date: date
    template: Template
    active_goals: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredDraftOutcome:
    draft: StructuredDraft
    fidelity: FidelityReport
    attempts: int
    pseudonymized: bool


class UrllibLLMTransport:
    """Async wrapper over stdlib HTTP POST for Ollama or OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        base_url: str,
        model: str,
        api_key: str | None = None,
        cloud_processing_enabled: bool = False,
        timeout_seconds: int = 60,
    ) -> None:
        if provider == "cloud" and not cloud_processing_enabled:
            raise PermissionError(
                "Cloud LLM structuring requires explicit CAREVOICE_CLOUD_PROCESSING_ENABLED=true"
            )
        if provider == "cloud" and not (api_key or "").strip():
            raise ValueError("Cloud LLM API key is required when llm_provider=cloud")
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def complete_json(self, messages: Sequence[dict[str, str]]) -> str:
        return await asyncio.to_thread(self._post_sync, messages)

    def _post_sync(self, messages: Sequence[dict[str, str]]) -> str:
        if self._provider == "ollama":
            url = f"{self._base_url}/api/chat"
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": list(messages),
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            }
            headers = {"Content-Type": "application/json"}
        else:
            url = f"{self._base_url}/chat/completions"
            payload = {
                "model": self._model,
                "messages": list(messages),
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }

        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("LLM provider request failed") from exc

        if self._provider == "ollama":
            content = body.get("message", {}).get("content")
        else:
            choices = body.get("choices") or []
            content = choices[0].get("message", {}).get("content") if choices else None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM provider returned an empty response")
        return content


class ClinicalStructurer:
    """Structure a transcript into a strict, source-grounded StructuredDraft."""

    def __init__(
        self,
        transport: LLMTransport,
        *,
        pseudonymize_before_llm: bool = True,
        max_corrective_retries: int = 2,
    ) -> None:
        if max_corrective_retries < 0 or max_corrective_retries > 5:
            raise ValueError("max_corrective_retries must be between 0 and 5")
        self._transport = transport
        self._pseudonymize = pseudonymize_before_llm
        self._max_retries = max_corrective_retries

    async def structure(
        self,
        transcript: str,
        context: StructuringContext,
    ) -> StructuredDraftOutcome:
        if not transcript.strip():
            raise ValueError("Transcript cannot be empty")

        if self._pseudonymize:
            pseudo = pseudonymize_transcript(
                transcript,
                patient_name=context.patient_name,
                patient_alias=context.patient_alias,
                guardian_names=context.guardian_names,
            )
            prompt_transcript = pseudo.text
            mapping = pseudo.mapping
        else:
            prompt_transcript = transcript
            mapping = None

        messages: list[dict[str, str]] = [
            {"role": "system", "content": _system_prompt(context)},
            {"role": "user", "content": _user_prompt(prompt_transcript, context)},
        ]

        last_error = "Resposta não estruturada."
        total_attempts = 1 + self._max_retries
        for attempt in range(1, total_attempts + 1):
            raw_output = await self._transport.complete_json(messages)
            cleaned_json = _extract_json_object(raw_output)
            try:
                draft = StructuredDraft.from_json(cleaned_json)
            except (ValidationError, ValueError) as exc:
                last_error = f"Erro de schema JSON: {exc}"
                messages.append({"role": "assistant", "content": raw_output})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "O JSON retornado violou o schema estrito. Corrija e retorne APENAS "
                            f"JSON válido conforme as regras. Detalhe: {last_error}"
                        ),
                    }
                )
                continue

            if mapping is not None:
                draft = restore_draft_pseudonyms(draft, mapping)
            draft = filter_internal_parent_summary(draft)

            if (
                draft.patient_id != context.patient_id
                or draft.session_date != context.session_date
                or draft.template != context.template
            ):
                last_error = "Metadados patient_id, session_date ou template divergem da sessão."
                messages.append({"role": "assistant", "content": raw_output})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Corrija os campos de cabeçalho: patient_id='{context.patient_id}', "
                            f"session_date='{context.session_date.isoformat()}', "
                            f"template='{context.template}'."
                        ),
                    }
                )
                continue

            fidelity = verify_draft_fidelity(draft, transcript)
            if fidelity.grounded:
                return StructuredDraftOutcome(
                    draft=draft,
                    fidelity=fidelity,
                    attempts=attempt,
                    pseudonymized=self._pseudonymize,
                )

            issue_summary = ", ".join(f"{issue.code}@{issue.path}" for issue in fidelity.issues[:8])
            last_error = f"Falha de fidelidade factual: {issue_summary}"
            messages.append({"role": "assistant", "content": raw_output})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Algumas afirmações ou medidas não possuem citação literal presente na "
                        f"transcrição ({issue_summary}). Remova fatos não ditos ou ajuste cada "
                        "evidence[].quote para trecho LITERAL da transcrição. Retorne apenas JSON."
                    ),
                }
            )

        raise StructuringFailed(
            f"Não foi possível estruturar rascunho fiel após {total_attempts} tentativa(s): "
            f"{last_error}"
        )


def _system_prompt(context: StructuringContext) -> str:
    template_hint = _template_schema_hint(context.template)
    return (
        "Você é um assistente de documentação clínica em português (pt-BR). "
        "Estruture EXCLUSIVAMENTE o que foi dito na transcrição, sem diagnosticar, prescrever "
        "ou inventar números, unidades ou falas.\n"
        "Regras obrigatórias:\n"
        "1. Toda afirmação (Claim) e medida (Measure) precisa de `evidence` com `quote` LITERAL "
        "extraído da transcrição.\n"
        "2. Trechos marcados com `#interno` NUNCA devem aparecer em `parent_summary`.\n"
        "3. Retorne SOMENTE um objeto JSON válido compatível com o template solicitado.\n"
        f"{template_hint}"
    )


def _user_prompt(transcript: str, context: StructuringContext) -> str:
    goals_text = (
        "\n".join(f"- {goal}" for goal in context.active_goals)
        if context.active_goals
        else "Nenhuma meta ativa informada."
    )
    return (
        f"patient_id: {context.patient_id}\n"
        f"session_date: {context.session_date.isoformat()}\n"
        f"template: {context.template}\n"
        f"Metas ativas:\n{goals_text}\n\n"
        f"Transcrição:\n{transcript}"
    )


def _template_schema_hint(template: Template) -> str:
    common_measure = (
        'measures: [{"value": number>0, "unit": "percentage|seconds|minutes|count|scale", '
        '"is_approximate": bool, "original_expression": str, "goal_id": str|null, '
        '"evidence": [{"quote": str}]}]'
    )
    claim_shape = '{"text": str, "evidence": [{"quote": str}]}'
    match template:
        case "SOAP":
            return (
                '{"patient_id": str, "session_date": "YYYY-MM-DD", "template": "SOAP", '
                '"content": {"kind": "SOAP", "subjective": [Claim], "objective": [Claim], '
                '"assessment": [Claim], "plan": [Claim], "parent_summary": [Claim], '
                f"{common_measure}}}"
                f" onde Claim={claim_shape}."
            )
        case "ABC":
            return (
                '{"patient_id": str, "session_date": "YYYY-MM-DD", "template": "ABC", '
                '"content": {"kind": "ABC", "episodes": [{"antecedent": Claim, '
                '"behavior": Claim, "consequence": Claim, "hypothesized_function": Claim|null}], '
                f'"parent_summary": [Claim], {common_measure}}}'
                f" onde Claim={claim_shape}."
            )
        case "FREE":
            return (
                '{"patient_id": str, "session_date": "YYYY-MM-DD", "template": "FREE", '
                '"content": {"kind": "FREE", "sections": [{"title": str, "claims": [Claim]}], '
                f'"parent_summary": [Claim], {common_measure}}}'
                f" onde Claim={claim_shape}."
            )


def _extract_json_object(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
