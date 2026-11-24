"""
Stage 1 of the viral-clip pipeline.

Downloads a YouTube video at highest quality with yt-dlp, then transcribes it
locally with sherpa-onnx/Parakeet (default) or faster-whisper (word-level
timestamps). Writes work/transcript.json.

No external/paid APIs. CPU-friendly. Usage:
    python download_and_transcribe.py <youtube_url> --asr parakeet
"""
import sys
import json
import argparse
import os
import subprocess
import tempfile
import shutil
import threading
import html
import math
import re
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlparse
from pathlib import Path

TRANSCRIPT_VERSION = 2
CHUNK_SECONDS = 1800
OVERLAP_SECONDS = 30


def run(cmd):
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True)


def resolve_source(source, workdir):
    """Keep local files in place; only network URLs go through yt-dlp."""
    path = Path(source).expanduser()
    if path.is_file():
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}:
            raise ValueError("Use a video file: MP4, MKV, WebM, MOV, M4V or AVI.")
        return path.resolve()
    if urlparse(source).scheme not in {"http", "https"}:
        raise ValueError(f"Video file does not exist or URL is invalid: {source}")
    return download(source, Path(workdir))


def download(url, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents a changed URL from reusing an old source.mp4.
    with tempfile.TemporaryDirectory(prefix="download-", dir=workdir) as temp:
        video = _download(url, Path(temp))
        destination = workdir / video.name
        shutil.move(str(video), str(destination))
        return destination.resolve()


def _download(url, workdir):
    out_tmpl = workdir / "source.%(ext)s"
    # Highest quality: best video + best audio, merged to mp4 (needs ffmpeg on PATH).
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(out_tmpl),
        url,
    ]
    run(cmd)
    vids = [p for p in sorted(workdir.glob("source.*"))
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")]
    if not vids:
        raise SystemExit("Download failed: no video file produced.")
    return vids[0].resolve()


def _use_fast_asr(asr_backend):
    """Use the sherpa-onnx/Parakeet backend when asked and available."""
    if asr_backend not in ("parakeet", "auto"):
        return False
    try:
        import fast_asr
        return fast_asr.available()
    except Exception:
        return False


def _fast_transcribe_media(video, offset=0.0, wav=None, max_workers=None):
    """Transcribe with Parakeet. `wav` may be a pre-extracted 16 kHz mono file."""
    import fast_asr
    pre_extracted = wav is not None
    temporary = None
    if wav is None:
        temporary = Path(tempfile.mkdtemp(prefix="chopify-asr-")) / "audio.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(temporary)], check=True)
        wav = temporary
    try:
        result = fast_asr.transcribe(wav, max_workers=max_workers)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary.parent, ignore_errors=True)
    words = [{"word": w["word"], "start": round(w["start"] + offset, 2),
              "end": round(w["end"] + offset, 2)} for w in result["words"]]
    seg_list = [{"start": round(s["start"] + offset, 2),
                 "end": round(s["end"] + offset, 2), "text": s["text"]}
                for s in result["segments"]]
    if words:
        info_duration = max(w["end"] for w in words)
    elif not pre_extracted:
        try:
            info_duration = _probe_duration(video)
        except (OSError, ValueError):
            info_duration = 0.0
    else:
        info_duration = 0.0
    return seg_list, words, result["language"], info_duration


def _caption_url_allowed(url):
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.netloc in {"www.youtube.com", "youtube.com"}
                and value.path == "/api/timedtext" and "tlang" not in parse_qs(value.query))
    except (TypeError, ValueError):
        return False


class _CaptionRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, next_url):
        if not _caption_url_allowed(next_url):
            raise ValueError("Unapproved caption redirect")
        return super().redirect_request(request, file, code, message, headers, next_url)


def _parse_source_captions(payload, duration):
    """Read JSON3 word offsets, estimating only words within phrase-only cues."""
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return [], [], False
    words = []
    estimated = False
    for event in payload["events"]:
        if not isinstance(event, dict):
            continue
        try:
            raw_start = float(event.get("tStartMs", 0)) / 1000
            cue_duration = float(event.get("dDurationMs", 0)) / 1000
            if not math.isfinite(raw_start) or not math.isfinite(cue_duration):
                continue
            origin = max(0, raw_start)
            edge = min(duration, origin + cue_duration)
            fragments = [f for f in event.get("segs", []) if isinstance(f, dict)
                         and isinstance(f.get("utf8"), str) and f["utf8"].strip()]
            if not math.isfinite(origin) or not math.isfinite(edge) or edge <= origin:
                continue
            for index, fragment in enumerate(fragments):
                tokens = html.unescape(fragment["utf8"]).split()
                if not tokens:
                    continue
                start = origin + float(fragment.get("tOffsetMs", (edge-origin)*1000*index/len(fragments))) / 1000
                following = fragments[index + 1] if index + 1 < len(fragments) else None
                stop = (origin + float(following.get("tOffsetMs", (edge-origin)*1000*(index+1)/len(fragments))) / 1000
                        if following else edge)
                if not following and (len(fragments) > 1 or "tOffsetMs" in fragment):
                    stop = min(stop, start + max(.45, .34 * len(tokens)))
                if not math.isfinite(start) or not math.isfinite(stop):
                    continue
                start, stop = max(origin, start), min(edge, stop)
                if stop <= start:
                    continue
                estimated |= len(tokens) > 1 or "tOffsetMs" not in fragment
                for position, token in enumerate(tokens):
                    a = start + (stop-start)*position/len(tokens)
                    b = start + (stop-start)*(position+1)/len(tokens)
                    if b-a > .001:
                        words.append({"word": token, "start": a, "end": b})
        except (ValueError, TypeError, OverflowError):
            continue
    ordered = []
    for word in sorted(words, key=lambda item: (item["start"], item["end"])):
        if ordered and word["start"] - ordered[-1]["start"] < .08 and word["word"] == ordered[-1]["word"]:
            continue
        if not ordered or word["start"] >= ordered[-1]["start"] + .008:
            ordered.append(word)
    for current, following in zip(ordered, ordered[1:]):
        current["end"] = min(current["end"], following["start"])
    ordered = [word for word in ordered if word["end"] > word["start"]]
    segments = []
    group = []
    for index, word in enumerate(ordered):
        group.append(word)
        following = ordered[index+1] if index+1 < len(ordered) else None
        if not following or following["start"]-word["end"] >= .65 or re.search(r"[.!?。！？]$", word["word"]):
            segments.append({"start": group[0]["start"], "end": group[-1]["end"],
                             "text": " ".join(item["word"] for item in group)})
            group = []
    return ordered, segments, estimated


def _source_caption_transcript(source, video, workdir, duration, media_kind,
                               render_from_sections, identity):
    """Try public source captions before loading a speech model."""
    try:
        parsed = urlsplit(str(source or ""))
        if parsed.scheme not in {"http", "https"} or parsed.netloc not in {
                "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            return None
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-single-json",
               "--no-playlist", "--no-warnings", "--socket-timeout", "10", "--retries", "0",
               "--cache-dir", str(workdir / "ytdlp-cache"), str(source)]
        if shutil.which("node"):
            cmd[3:3] = ["--js-runtimes", "node", "--proxy", ""]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                check=True, timeout=90)
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            return None
        total = float(duration or info.get("duration") or 0)
        if not math.isfinite(total) or total <= 0:
            return None
        native = str(info.get("language") or "").split("-")[0]
        tracks = []
        for mapping in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
            if not isinstance(mapping, dict):
                continue
            mapping = {key: value for key, value in mapping.items()
                       if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9-]{2,24}", key) and isinstance(value, list)}
            keys = sorted(mapping, key=lambda key: (key.split("-")[0] != native, key.endswith("-orig") is False))
            for language in keys:
                for track in mapping[language]:
                    if isinstance(track, dict) and track.get("ext") == "json3" and _caption_url_allowed(track.get("url")):
                        tracks.append((language.removesuffix("-orig"), track["url"]))
        opener = build_opener(ProxyHandler({}), _CaptionRedirects())
        for language, url in tracks[:3]:
            try:
                with opener.open(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    continue
                words, segments, estimated = _parse_source_captions(json.loads(body), total)
                if not words:
                    continue
                transcript = {"video": str(video), "language": language, "duration": total,
                              "asr": "youtube-captions", "model": "source-captions", "segments": segments,
                              "words": words, "transcript_version": TRANSCRIPT_VERSION, "chunked": False,
                              "chunks": 0, "media_kind": media_kind,
                              "render_from_sections": bool(render_from_sections), "source": str(source),
                              "source_duration": total, "source_identity": str(identity or ""),
                              "caption_timing": "estimated-within-phrases" if estimated else "word-offsets"}
                out = workdir / "transcript.json"
                _atomic_json(out, transcript)
                print(f"Used source captions ({language}, {len(words)} words).", flush=True)
                return out
            except (OSError, ValueError, TypeError):
                continue
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass
    print("Source captions unavailable; using local speech transcription.", flush=True)
    return None


def transcribe(video, workdir, model_size, device, compute_type, duration=None,
               chunked=False, source=None, media_kind="video",
               render_from_sections=False, identity=None, progress=None,
               resume=True, asr_backend="whisper"):
    """Transcribe a short source in one pass or a long source in chunks."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if asr_backend == "none":
        if duration is None:
            try:
                duration = _probe_duration(video)
            except (OSError, ValueError):
                duration = 0.0
        return write_media_transcript(
            video, workdir, duration, source=source, media_kind=media_kind,
            render_from_sections=render_from_sections, identity=identity)
    captions = _source_caption_transcript(source, video, workdir, duration, media_kind,
                                          render_from_sections, identity)
    if captions is not None:
        return captions
    if chunked:
        return transcribe_chunked(
            video, workdir, model_size, device, compute_type, duration,
            source=source, media_kind=media_kind,
            render_from_sections=render_from_sections, identity=identity,
            progress=progress, resume=resume, asr_backend=asr_backend)

    fast = _use_fast_asr(asr_backend)
    if fast:
        print("Transcribing with Parakeet (sherpa-onnx, CPU).", flush=True)
        seg_list, words, language, info_duration = _fast_transcribe_media(video)
        model_label = "parakeet"
    else:
        if asr_backend == "parakeet":
            print("sherpa-onnx unavailable; falling back to faster-whisper. "
                  "Install it with: pip install -r requirements.txt", flush=True)
        model = _load_model(model_size, device, compute_type)
        seg_list, words, language, info_duration = _transcribe_media(model, video)
        model_label = model_size
    total = float(duration) if duration is not None else float(info_duration)
    transcript = {
        "video": str(video),
        "language": language,
        "duration": total,
        "asr": "parakeet" if fast else "faster-whisper",
        "model": model_label,
        "segments": seg_list,
        "words": words,
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": False,
        "chunks": 0,
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": str(identity or ""),
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  ({len(words)} words, {len(seg_list)} segments)", flush=True)
    return out


def _atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(temp, path)


def _load_model(model_size, device, compute_type):
    from faster_whisper import WhisperModel
    print(f"Loading faster-whisper model={model_size} device={device} "
          f"compute={compute_type} (first run downloads the model)...", flush=True)
    return WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=3, num_workers=1, download_root=str(Path(__file__).resolve().parents[2] / "workspace" / "studio" / "models"))


def _transcribe_media(model, media, offset=0.0):
    segments, info = model.transcribe(
        str(media), word_timestamps=True, vad_filter=True)
    language = getattr(info, "language", "")
    print(f"Detected language: {language} "
          f"(p={getattr(info, 'language_probability', 0.0):.2f}), "
          f"audio {getattr(info, 'duration', 0.0):.1f}s", flush=True)
    seg_list, words = [], []
    for seg in segments:
        start = float(seg.start) + offset
        end = float(seg.end) + offset
        seg_list.append({"start": start, "end": end, "text": seg.text})
        for w in (seg.words or []):
            words.append({
                "word": w.word,
                "start": float(w.start) + offset,
                "end": float(w.end) + offset,
            })
        print(f"[{start:7.2f} -> {end:7.2f}] {seg.text}", flush=True)
    info_duration = float(getattr(info, "duration", 0.0) or 0.0)
    return seg_list, words, language, info_duration


def _probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def _extract_chunk(source, start, end, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{float(start):.3f}", "-t", f"{float(end) - float(start):.3f}",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def _part_matches(part, index, start, end, identity):
    if not isinstance(part, dict):
        return False
    if part.get("version") != TRANSCRIPT_VERSION:
        return False
    if part.get("index") != index:
        return False
    if str(part.get("identity") or "") != str(identity or ""):
        return False
    try:
        if abs(float(part.get("start")) - float(start)) > 1e-3:
            return False
        if abs(float(part.get("end")) - float(end)) > 1e-3:
            return False
    except (TypeError, ValueError):
        return False
    words = part.get("words") or []
    starts = [float(word.get("start", 0.0)) for word in words]
    return all(a <= b for a, b in zip(starts, starts[1:]))


def _read_part(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def merge_transcript_parts(parts, duration, overlap=OVERLAP_SECONDS):
    """Merge overlapping chunk transcripts into one global timeline."""
    ordered = sorted(parts, key=lambda part: int(part.get("index", 0)))
    last = len(ordered) - 1
    merged_words, merged_segments = [], []
    language = ""
    for position, part in enumerate(ordered):
        start = float(part.get("start") or 0.0)
        end = float(part.get("end") or 0.0)
        lower = start + float(overlap) / 2.0 if position > 0 else float("-inf")
        upper = end - float(overlap) / 2.0 if position < last else float("inf")
        merged_words.extend(
            word for word in (part.get("words") or [])
            if lower <= _midpoint(word) < upper)
        merged_segments.extend(
            seg for seg in (part.get("segments") or [])
            if lower <= _midpoint(seg) < upper)
        language = language or str(part.get("language") or "")

    words = _dedupe(sorted(merged_words, key=_start), "word")
    segments = _dedupe(sorted(merged_segments, key=_start), "text")
    return {"words": words, "segments": segments, "language": language,
            "chunks": len(ordered), "duration": float(duration)}


def _midpoint(item):
    return (float(item.get("start", 0.0)) + float(item.get("end", 0.0))) / 2.0


def _start(item):
    return float(item.get("start", 0.0))


def _dedupe(items, key):
    seen = set()
    out = []
    for item in items:
        marker = (round(_start(item), 3), str(item.get(key) or ""))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def transcribe_chunked(video, workdir, model_size, device, compute_type,
                       duration=None, source=None, media_kind="video",
                       render_from_sections=False, identity=None,
                       progress=None, resume=True, asr_backend="whisper"):
    """Transcribe a long source in 30-minute parts with overlap."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    source_path = Path(video)
    total = float(duration) if duration is not None else _probe_duration(source_path)
    if total <= 0:
        raise ValueError("Cannot transcribe an empty source.")
    identity = str(identity or "")
    if asr_backend == "none":
        return write_media_transcript(
            video, workdir, total, source=source, media_kind=media_kind,
            render_from_sections=render_from_sections, identity=identity)

    bounds = []
    index = 0
    while index * CHUNK_SECONDS < total:
        start = index * CHUNK_SECONDS
        owned_end = min(total, start + CHUNK_SECONDS)
        end = min(total, owned_end + OVERLAP_SECONDS)
        bounds.append((index, float(start), float(end)))
        index += 1

    fast = _use_fast_asr(asr_backend)
    if fast:
        print("Transcribing with Parakeet (sherpa-onnx, CPU).", flush=True)
    elif asr_backend == "parakeet":
        print("sherpa-onnx unavailable; falling back to faster-whisper. "
              "Install it with: pip install -r requirements.txt", flush=True)
    model = None
    part_dir = workdir / "audio_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    progress_lock = threading.Lock()
    completed = [0]
    total_parts = len(bounds)

    def cached_part(index, start, end):
        part = (_read_part(workdir / f"transcript.part{index:03d}.json")
                if resume else None)
        return part if _part_matches(part, index, start, end, identity) else None

    def process_part(index, start, end):
        part = cached_part(index, start, end)
        if part is None:
            wav = part_dir / f"part{index:03d}.wav"
            _extract_chunk(source_path, start, end, wav)
            if fast:
                seg_list, words, language, _ = _fast_transcribe_media(
                    source_path, offset=start, wav=wav, max_workers=1)
            else:
                seg_list, words, language, _ = _transcribe_media(
                    model, wav, offset=start)
            part = {
                "version": TRANSCRIPT_VERSION,
                "index": index,
                "start": start,
                "end": end,
                "identity": identity,
                "language": language,
                "segments": seg_list,
                "words": words,
            }
            _atomic_json(workdir / f"transcript.part{index:03d}.json", part)
            try:
                wav.unlink()
            except OSError:
                pass
        with progress_lock:
            completed[0] += 1
            if progress:
                progress(completed[0], total_parts)
        return part

    if not fast and any(cached_part(*b) is None for b in bounds):
        model = _load_model(model_size, device, compute_type)

    if fast:
        import fast_asr
        parallel = max(1, int(os.environ.get("CHOPIFY_ASR_PARTS")
                              or fast_asr.COPIES))
        if parallel > 1 and total_parts > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=parallel,
                                    thread_name_prefix="chopify-parts") as pool:
                parts = list(pool.map(lambda b: process_part(*b), bounds))
        else:
            parts = [process_part(*b) for b in bounds]
    else:
        parts = [process_part(*b) for b in bounds]

    merged = merge_transcript_parts(parts, total, overlap=OVERLAP_SECONDS)
    transcript = {
        "video": str(video),
        "language": merged["language"],
        "duration": total,
        "asr": "parakeet" if fast else "faster-whisper",
        "model": "parakeet" if fast else model_size,
        "segments": merged["segments"],
        "words": merged["words"],
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": True,
        "chunks": merged["chunks"],
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": identity,
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  ({len(transcript['words'])} words, "
          f"{len(transcript['segments'])} segments, {merged['chunks']} chunks)",
          flush=True)
    return out


def write_media_transcript(video, workdir, duration, source=None,
                           media_kind="video", render_from_sections=True,
                           identity=None, language=""):
    """Write an empty transcript for a silent source scored from media alone."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    total = float(duration or 0.0)
    transcript = {
        "video": str(video),
        "language": language,
        "duration": total,
        "asr": "none",
        "model": None,
        "segments": [],
        "words": [],
        "transcript_version": TRANSCRIPT_VERSION,
        "chunked": False,
        "chunks": 0,
        "media_kind": media_kind,
        "render_from_sections": bool(render_from_sections),
        "source": str(source or video),
        "source_duration": total,
        "source_identity": str(identity or ""),
    }
    out = workdir / "transcript.json"
    _atomic_json(out, transcript)
    print(f"Wrote {out}  (media-only source, no speech)", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--model", default="medium",
                    help="faster-whisper model: tiny/base/small/medium/large-v3")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--asr", choices=["parakeet", "whisper", "auto", "none"],
                    default="parakeet", help="speech engine (default: parakeet)")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    video = resolve_source(args.url, workdir)
    print("Downloaded:", video, flush=True)
    transcribe(video, workdir, args.model, args.device, args.compute_type,
               asr_backend=args.asr)
    print("STAGE 1 COMPLETE", flush=True)


if __name__ == "__main__":
    main()
