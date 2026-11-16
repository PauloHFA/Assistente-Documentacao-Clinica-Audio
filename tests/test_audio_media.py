import io
import math
import struct
import wave

import pytest

from carevoice.audio.limits import AudioValidationError, validate_audio_upload


def _make_wav_bytes(*, frequency: float, amplitude: int, duration_s: float = 0.25) -> bytes:
    sample_rate = 16000
    num_samples = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            val = int(amplitude * math.sin(2.0 * math.pi * frequency * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def test_validate_audio_upload_inspects_real_wav_and_rejects_silence() -> None:
    voiced = _make_wav_bytes(frequency=440.0, amplitude=12000, duration_s=0.3)
    validated = validate_audio_upload(
        "session.wav",
        voiced,
        duration_seconds=1.0,
        content_type="audio/wav",
        verify_media=True,
    )
    assert validated.format == "wav"
    assert 0.2 <= validated.duration_seconds <= 0.4

    silent = _make_wav_bytes(frequency=440.0, amplitude=0, duration_s=0.3)
    with pytest.raises(AudioValidationError, match="no detectable speech"):
        validate_audio_upload(
            "silent.wav",
            silent,
            duration_seconds=1.0,
            content_type="audio/wav",
            verify_media=True,
        )
