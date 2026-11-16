"""Opt-in cloud transcription adapter using OpenAI-compatible multipart endpoints."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from carevoice.transcribe.base import AudioFormat, TranscriptionResult


class CloudTranscriptionTransport(Protocol):
    async def transcribe_multipart(
        self,
        *,
        payload: bytes,
        audio_format: AudioFormat,
        model: str,
        prompt: str | None,
    ) -> TranscriptionResult: ...


@dataclass(frozen=True)
class UrllibCloudTranscriptionTransport:
    """Multipart HTTP client using stdlib urllib without retaining local temp files."""

    base_url: str
    api_key: str
    timeout_seconds: int = 90

    async def transcribe_multipart(
        self,
        *,
        payload: bytes,
        audio_format: AudioFormat,
        model: str,
        prompt: str | None,
    ) -> TranscriptionResult:
        return await asyncio.to_thread(
            self._post_sync,
            payload,
            audio_format,
            model,
            prompt,
        )

    def _post_sync(
        self,
        payload: bytes,
        audio_format: AudioFormat,
        model: str,
        prompt: str | None,
    ) -> TranscriptionResult:
        boundary = f"----CareVoiceBoundary{uuid4().hex}"
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            )

        add_field("model", model)
        add_field("language", "pt")
        add_field("response_format", "verbose_json")
        if prompt:
            add_field("prompt", prompt)

        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.{audio_format}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + payload
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        url = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cloud transcription request failed") from exc

        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Cloud transcriber returned empty transcription")
        language = data.get("language")
        return TranscriptionResult(
            text=text.strip(),
            language=language if isinstance(language, str) and language.strip() else "pt",
        )


class CloudOpenAITranscriber:
    """Cloud transcription adapter gated by explicit cloud_processing_enabled=True."""

    def __init__(
        self,
        *,
        cloud_processing_enabled: bool,
        model: str = "whisper-1",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        transport: CloudTranscriptionTransport | None = None,
    ) -> None:
        if not cloud_processing_enabled:
            raise PermissionError(
                "Cloud transcription requires explicit CAREVOICE_CLOUD_PROCESSING_ENABLED=true"
            )
        if transport is None:
            if not (api_key or "").strip():
                raise ValueError("Cloud transcriber API key is required")
            transport = UrllibCloudTranscriptionTransport(
                base_url=base_url,
                api_key=api_key or "",
            )
        self._model = model
        self._transport = transport

    async def transcribe(
        self,
        payload: bytes,
        audio_format: AudioFormat,
        glossary_terms: Sequence[str] = (),
    ) -> TranscriptionResult:
        if not payload:
            raise ValueError("Audio payload cannot be empty")
        if audio_format not in {"ogg", "mp3", "m4a", "wav"}:
            raise ValueError("Unsupported audio format")
        terms = [term.strip()[:80] for term in glossary_terms if term.strip()][:20]
        prompt = ("Vocabulário clínico: " + ", ".join(terms)) if terms else None
        return await self._transport.transcribe_multipart(
            payload=payload,
            audio_format=audio_format,
            model=self._model,
            prompt=prompt,
        )
