"""Telegram Bot API HTTP polling adapter without exposing clinical content in logs."""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

import structlog

from carevoice.bot.router import BotResponse, CareVoiceBotHandler
from carevoice.config import Settings
from carevoice.db.database import Database
from carevoice.domain.identity import IdentityService
from carevoice.domain.records import ClinicalRecordService
from carevoice.jobs.queue import JobQueue
from carevoice.logging import configure_logging
from carevoice.security.crypto import FieldCipher

_logger = structlog.get_logger(__name__)


class TelegramApiClient:
    """Minimal Telegram Bot API client built on stdlib urllib."""

    def __init__(self, token: str, *, timeout_seconds: int = 35) -> None:
        if not token.strip():
            raise ValueError("Telegram bot token cannot be empty")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}"
        self._timeout = timeout_seconds

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post_json("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send_response(self, chat_id: str, response: BotResponse) -> None:
        reply_markup: dict[str, Any] | None = None
        if response.buttons:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": btn.text, "callback_data": btn.callback_data} for btn in row]
                    for row in response.buttons
                ]
            }
        payload: dict[str, Any] = {"chat_id": chat_id, "text": response.text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._post_json("sendMessage", payload)

        if response.document is not None:
            self._send_document(
                chat_id=chat_id,
                filename=response.document.filename,
                content=response.document.content,
                caption=response.document.caption,
            )

    def answer_callback_query(self, callback_query_id: str) -> None:
        self._post_json("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        file_meta = self._post_json("getFile", {"file_id": file_id})
        if not isinstance(file_meta, dict) or not isinstance(file_meta.get("file_path"), str):
            raise RuntimeError("Telegram getFile did not return a file_path")
        file_path = file_meta["file_path"]
        req = urllib.request.Request(f"{self._file_base_url}/{file_path}", method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read(), file_path

    def _post_json(self, method: str, payload: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram API call failed: {method}") from exc
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}")
        return data.get("result")

    def _send_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str,
    ) -> None:
        boundary = f"----CareVoiceTg{uuid4().hex}"
        parts: list[bytes] = [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{chat_id}\r\n"
            ).encode()
        ]
        if caption:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="caption"\r\n\r\n'
                    f"{caption}\r\n"
                ).encode()
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            f"{self._base_url}/sendDocument",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                raise RuntimeError("Telegram sendDocument failed")


class TelegramBotRunner:
    """Long-polling loop bridging Telegram updates into CareVoiceBotHandler."""

    def __init__(self, client: TelegramApiClient, handler: CareVoiceBotHandler) -> None:
        self._client = client
        self._handler = handler

    def process_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            data = str(callback.get("data", ""))
            query_id = str(callback.get("id", ""))
            if chat_id and data:
                response = self._handler.handle_callback(chat_id, data)
                self._client.send_response(chat_id, response)
            if query_id:
                self._client.answer_callback_query(query_id)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        if not chat_id:
            return

        voice_or_audio = message.get("voice") or message.get("audio")
        if isinstance(voice_or_audio, dict) and isinstance(voice_or_audio.get("file_id"), str):
            payload, file_path = self._client.download_file(voice_or_audio["file_id"])
            duration = float(voice_or_audio.get("duration") or 1.0)
            mime_type = voice_or_audio.get("mime_type")
            filename = (
                voice_or_audio.get("file_name") or file_path.rsplit("/", 1)[-1] or "voice.ogg"
            )
            if "." not in filename:
                filename = f"{filename}.ogg"
            caption = str(message.get("caption") or "").strip()
            response = self._handler.handle_audio(
                chat_id,
                patient_hint=caption,
                filename=filename,
                payload=payload,
                duration_seconds=duration,
                content_type=mime_type if isinstance(mime_type, str) else None,
            )
            self._client.send_response(chat_id, response)
            return

        text = message.get("text")
        if isinstance(text, str):
            response = self._handler.handle_text(chat_id, text)
            self._client.send_response(chat_id, response)

    async def run(self, stop_event: asyncio.Event) -> None:
        offset: int | None = None
        while not stop_event.is_set():
            try:
                updates = await asyncio.to_thread(self._client.get_updates, offset=offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    await asyncio.to_thread(self.process_update, update)
            except Exception as exc:
                _logger.warning("telegram_poll_error", error_type=type(exc).__name__)
                await asyncio.sleep(2.0)


def main() -> None:
    """Run the Telegram bot polling service using environment settings."""
    settings = Settings()
    if settings.telegram_bot_token is None:
        raise ValueError("CAREVOICE_TELEGRAM_BOT_TOKEN is required to run the bot")
    if settings.field_encryption_key is None:
        raise ValueError("CAREVOICE_FIELD_ENCRYPTION_KEY is required to run the bot")
    if settings.administrator_chat_id is None:
        raise ValueError("CAREVOICE_ADMINISTRATOR_CHAT_ID is required to run the bot")

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
        handler = CareVoiceBotHandler(
            identity,
            queue,
            records,
            real_data_enabled=settings.real_data_enabled,
        )
        runner = TelegramBotRunner(
            TelegramApiClient(settings.telegram_bot_token.get_secret_value()),
            handler,
        )
        asyncio.run(runner.run(asyncio.Event()))
    finally:
        database.dispose()
