"""CPU speech recognition and time-aligned audio measurements."""

import math
import os
import wave
from functools import lru_cache


ENERGY_STEP = 0.5
DEFAULT_MODEL = "tiny"


def selected_model():
    return os.getenv("CHAOTICCLIPS_WHISPER_MODEL", DEFAULT_MODEL).lower()


def audio_energy(path):
    """Measure half-second RMS levels in dBFS from a mono PCM analysis WAV."""
    import numpy as np

    levels = []
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Audio analysis requires 16-bit mono PCM.")
        frame_count = max(1, round(source.getframerate() * ENERGY_STEP))
        while samples := source.readframes(frame_count):
            values = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
            mean_square = float(np.mean(values * values))
            levels.append(round(10 * math.log10(max(mean_square, 1e-8)), 2))
    return levels


@lru_cache(maxsize=1)
def _model(model_size, cache_dir):
    # Hugging Face's optional Xet client keeps its own cache. The HTTP client
    # uses the explicit project download root passed to faster-whisper.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HOME"] = str(cache_dir)
    from faster_whisper import WhisperModel
    from huggingface_hub.errors import LocalEntryNotFoundError

    options = dict(device="cpu", compute_type="int8", num_workers=1,
                   cpu_threads=min(4, os.cpu_count() or 1), download_root=str(cache_dir), use_auth_token=False)
    try:
        return WhisperModel(model_size, local_files_only=True, **options)
    except LocalEntryNotFoundError:
        return WhisperModel(model_size, **options)


def transcribe(path, language, cache_dir, progress=None):
    """Transcribe an analysis WAV and preserve faster-whisper's word timing."""
    model_size = selected_model()
    if model_size not in {"tiny", "base"}:
        raise ValueError("The CPU speech model must be tiny or base.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0)

    try:
        model = _model(model_size, cache_dir.resolve())
    except Exception as error:
        raise RuntimeError("Could not load the local speech model. First use needs access to Hugging Face.") from error

    try:
        segments, info = model.transcribe(
            str(path), language=None if language == "auto" else language,
            word_timestamps=True, vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
            beam_size=3, condition_on_previous_text=False,
        )
        duration = max(info.duration, 1)
        last_progress = 0
        words = []
        for segment in segments:
            for word in segment.words or ():
                start, end = float(word.start), float(word.end)
                text = word.word.strip()
                if text and math.isfinite(start) and math.isfinite(end) and end > start:
                    words.append({"start": start, "end": end, "text": text})
            fraction = min(0.99, max(0, segment.end / duration))
            if progress and fraction - last_progress >= 0.03:
                progress(fraction)
                last_progress = fraction
    except Exception as error:
        raise RuntimeError("Local speech transcription failed. Check the source audio and spoken language.") from error

    if progress:
        progress(1)
    return words, info.language
