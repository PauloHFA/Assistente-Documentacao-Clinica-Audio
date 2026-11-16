import asyncio
import json
from io import BytesIO
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from carevoice.bot.router import BotResponse, CareVoiceBotHandler, DocumentAttachment, InlineButton
from carevoice.bot.telegram_api import TelegramApiClient, TelegramBotRunner
from carevoice.db.database import Database
from carevoice.domain.identity import IdentityService
from carevoice.domain.records import ClinicalRecordService
from carevoice.jobs.queue import JobQueue
from carevoice.security.crypto import FieldCipher


class _FakeHttpResp:
    def __init__(self, body: bytes) -> None:
        self._buf = BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeHttpResp":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_telegram_api_client_methods() -> None:
    client = TelegramApiClient("123:token")
    responses = [
        _FakeHttpResp(json.dumps({"ok": True, "result": [{"update_id": 10}]}).encode()),
        _FakeHttpResp(json.dumps({"ok": True, "result": {"message_id": 1}}).encode()),
        _FakeHttpResp(json.dumps({"ok": True, "result": {"message_id": 2}}).encode()),
        _FakeHttpResp(json.dumps({"ok": True, "result": True}).encode()),
        _FakeHttpResp(json.dumps({"ok": True, "result": {"file_path": "voice/a.ogg"}}).encode()),
        _FakeHttpResp(b"audio-bytes"),
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        updates = client.get_updates(offset=9)
        assert updates == [{"update_id": 10}]
        client.send_response(
            "chat-1",
            BotResponse(
                text="ok",
                buttons=((InlineButton(text="Aprovar", callback_data="approve:1"),),),
                document=DocumentAttachment(filename="doc.pdf", content=b"%PDF-", caption="cap"),
            ),
        )
        client.answer_callback_query("cb-1")
        data, path = client.download_file("file-1")
        assert data == b"audio-bytes"
        assert path == "voice/a.ogg"


def test_telegram_api_client_rejects_empty_token_and_api_errors() -> None:
    with pytest.raises(ValueError):
        TelegramApiClient(" ")

    client = TelegramApiClient("123:token")
    with (
        patch(
            "urllib.request.urlopen",
            return_value=_FakeHttpResp(json.dumps({"ok": False}).encode()),
        ),
        pytest.raises(RuntimeError),
    ):
        client.get_updates()


def test_telegram_bot_runner_processes_text_voice_and_callbacks() -> None:
    db = Database("sqlite:///:memory:")
    db.create_schema_for_tests()
    try:
        cipher = FieldCipher(Fernet.generate_key().decode("ascii"))
        identity = IdentityService(db.sessions, cipher, "admin")
        code = identity.create_invitation(
            "admin", name="Dra", profession="Fono", council_registry="123"
        )
        identity.consume_invitation(code, "100")
        patient = identity.create_patient("100", display_name="Pedro")
        identity.grant_consent("100", patient.id, terms_version="v1", purpose="teste")
        queue = JobQueue(db.sessions, identity, cipher)
        records = ClinicalRecordService(db.sessions, cipher)
        handler = CareVoiceBotHandler(identity, queue, records)

        sent: list[tuple[str, str]] = []

        class FakeClient(TelegramApiClient):
            def __init__(self) -> None:
                super().__init__("123:token")

            def send_response(self, chat_id: str, response: BotResponse) -> None:
                sent.append((chat_id, response.text))

            def answer_callback_query(self, callback_query_id: str) -> None:
                sent.append(("cb", callback_query_id))

            def download_file(self, file_id: str) -> tuple[bytes, str]:
                return b"ogg-data", f"{file_id}.ogg"

        runner = TelegramBotRunner(FakeClient(), handler)
        runner.process_update({"message": {"chat": {"id": 100}, "text": "/status"}})
        runner.process_update(
            {
                "message": {
                    "chat": {"id": 100},
                    "voice": {"file_id": "v1", "duration": 5, "mime_type": "audio/ogg"},
                    "caption": "Pedro",
                }
            }
        )
        runner.process_update(
            {
                "callback_query": {
                    "id": "q1",
                    "data": "redo:abc",
                    "message": {"chat": {"id": 100}},
                }
            }
        )
        assert any("Sessão ativa" in msg for _, msg in sent)
        assert any("enfileirado" in msg for _, msg in sent)
        assert ("cb", "q1") in sent

        # Also exercise runner.run() loop stopping via stop_event
        class _LoopClient(FakeClient):
            def __init__(self, stop_ev: asyncio.Event) -> None:
                super().__init__()
                self._stop_ev = stop_ev

            def get_updates(
                self, *, offset: int | None = None, timeout: int = 25
            ) -> list[dict[str, object]]:
                del offset, timeout
                self._stop_ev.set()
                return [{"update_id": 42, "message": {"chat": {"id": 100}, "text": "/ajuda"}}]

        stop_event = asyncio.Event()
        asyncio.run(TelegramBotRunner(_LoopClient(stop_event), handler).run(stop_event))
        assert any("CareVoice AI" in msg for _, msg in sent)
    finally:
        db.dispose()
