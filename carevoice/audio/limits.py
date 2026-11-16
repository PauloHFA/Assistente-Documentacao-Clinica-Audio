"""Validation and container inspection for supported audio uploads."""

import math
from dataclasses import dataclass
from importlib import import_module
from io import BytesIO
from pathlib import PurePath
from typing import Any

_MIN_AUDIO_PEAK = 1e-4


class AudioValidationError(ValueError):
    """Raised when an audio upload exceeds limits, lacks speech energy, or is invalid."""


@dataclass(frozen=True)
class ValidatedAudio:
    payload: bytes
    format: str
    duration_seconds: float


@dataclass(frozen=True)
class InspectedAudio:
    duration_seconds: float
    peak_amplitude: float


_MIME_TYPES: dict[str, frozenset[str]] = {
    ".ogg": frozenset({"audio/ogg", "audio/opus", "application/ogg"}),
    ".mp3": frozenset({"audio/mpeg", "audio/mp3"}),
    ".m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    ".wav": frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
}


def inspect_audio_stream(payload: bytes) -> InspectedAudio:
    """Inspect real audio container duration and signal amplitude using PyAV (RF06)."""
    try:
        av_module: Any = import_module("av")
        np_module: Any = import_module("numpy")
    except ModuleNotFoundError as exc:
        raise RuntimeError("Audio inspection requires the [audio] extra (av, numpy)") from exc

    peak = 0.0
    duration_seconds = 0.0
    total_samples = 0
    sample_rate = 0
    try:
        with av_module.open(BytesIO(payload)) as container:
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if not audio_streams:
                raise AudioValidationError("Audio container does not include an audio stream")
            stream = audio_streams[0]
            if stream.duration is not None and stream.time_base is not None:
                duration_seconds = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration_seconds = float(container.duration / 1_000_000)

            for frame in container.decode(audio=0):
                sample_rate = frame.sample_rate or sample_rate
                samples = frame.samples or 0
                total_samples += samples
                arr = frame.to_ndarray()
                if arr.size:
                    if np_module.issubdtype(arr.dtype, np_module.integer):
                        info = np_module.iinfo(arr.dtype)
                        scale = float(max(abs(info.min), info.max)) or 1.0
                        frame_peak = float(np_module.max(np_module.abs(arr.astype(float))) / scale)
                    else:
                        frame_peak = float(np_module.max(np_module.abs(arr)))
                    if frame_peak > peak:
                        peak = frame_peak
    except AudioValidationError:
        raise
    except Exception as exc:
        raise AudioValidationError("Unable to decode audio payload") from exc

    if duration_seconds <= 0 and sample_rate > 0 and total_samples > 0:
        duration_seconds = total_samples / sample_rate
    if duration_seconds <= 0:
        raise AudioValidationError("Audio duration could not be determined")
    if peak < _MIN_AUDIO_PEAK:
        raise AudioValidationError("Audio contains no detectable speech or signal")
    return InspectedAudio(duration_seconds=duration_seconds, peak_amplitude=peak)


def validate_audio_upload(
    filename: str,
    payload: bytes,
    duration_seconds: float,
    *,
    max_bytes: int = 20 * 1024 * 1024,
    max_duration_seconds: int = 300,
    content_type: str | None = None,
    verify_media: bool = False,
) -> ValidatedAudio:
    """Validate metadata, size, and optionally real audio stream content."""
    suffix = PurePath(filename).suffix.casefold()
    if suffix not in _MIME_TYPES:
        raise AudioValidationError("Unsupported audio format")
    if content_type is not None and content_type.casefold() not in _MIME_TYPES[suffix]:
        raise AudioValidationError("Audio media type does not match its file extension")
    if not payload:
        raise AudioValidationError("Audio file is empty")
    if len(payload) > max_bytes:
        raise AudioValidationError(f"Audio exceeds the {max_bytes}-byte limit")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise AudioValidationError("Audio duration must be a positive finite number")
    effective_duration = duration_seconds
    if verify_media:
        inspected = inspect_audio_stream(payload)
        effective_duration = inspected.duration_seconds
    if effective_duration > max_duration_seconds:
        raise AudioValidationError(
            f"Audio exceeds the {max_duration_seconds}-second duration limit"
        )
    return ValidatedAudio(payload=payload, format=suffix[1:], duration_seconds=effective_duration)
