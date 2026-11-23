# app.py
import os
import re
import json
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None

try:
    from google import genai
except ImportError:
    genai = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "workspace"
UPLOAD_DIR = DATA_DIR / "uploads"
AUDIO_DIR = DATA_DIR / "audio"
PREVIEW_DIR = DATA_DIR / "previews"
TEMP_DIR = DATA_DIR / "temp"

for folder in (UPLOAD_DIR, AUDIO_DIR, PREVIEW_DIR, TEMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024

jobs = {}
sources = {}
clips = {}

lock = threading.RLock()
whisper_lock = threading.Lock()
whisper_model = None

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"
}


def json_error(message, status=400):
    return jsonify({"error": str(message)}), status


def run_command(args):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or "Media command failed.")
    return result


def ffmpeg_path():
    return shutil.which("ffmpeg")


def ffprobe_path():
    return shutil.which("ffprobe")


def require_media_tools():
    if not ffmpeg_path() or not ffprobe_path():
        raise RuntimeError(
            "FFmpeg and FFprobe are required. Install FFmpeg and add its bin folder to PATH."
        )


def safe_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid HTTP or HTTPS video URL.")
    return url


def format_size(size):
    if not size:
        return "Unknown"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def probe_video(path):
    require_media_tools()
    result = run_command([
        ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration,size:stream=width,height",
        "-of", "json", str(path)
    ])
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if "width" in s), {})
    return {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or 0),
        "width": video.get("width"),
        "height": video.get("height")
    }


def extract_metadata(url):
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed.")

    safe_url(url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("Could not read video information.")

    formats = info.get("formats") or []
    heights = sorted({
        f.get("height") for f in formats
        if f.get("height") and f.get("vcodec") != "none"
    }, reverse=True)

    estimated = info.get("filesize") or info.get("filesize_approx")
    if not estimated and info.get("duration"):
        estimated = int(info["duration"] * 180000)

    source_id = uuid.uuid4().hex
    sources[source_id] = {
        "kind": "url",
        "url": url,
        "title": info.get("title") or "Video",
        "duration": float(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "formats": formats
    }

    return {
        "source_id": source_id,
        "title": info.get("title") or "Video",
        "duration": float(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "width": info.get("width"),
        "height": info.get("height"),
        "available_heights": heights,
        "estimated_size": format_size(estimated)
    }


def get_whisper():
    global whisper_model
    if WhisperModel is None:
        raise RuntimeError("faster-whisper is not installed.")

    if whisper_model is None:
        with whisper_lock:
            if whisper_model is None:
                whisper_model = WhisperModel(
                    "tiny",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4
                )
    return whisper_model


def download_audio(source, source_id):
    audio_path = AUDIO_DIR / f"{source_id}.%(ext)s"

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(audio_path),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav"
        }]
    }

    with YoutubeDL(options) as ydl:
        ydl.download([source["url"]])

    wav_path = AUDIO_DIR / f"{source_id}.wav"
    if not wav_path.exists():
        matches = list(AUDIO_DIR.glob(f"{source_id}.*"))
        if not matches:
            raise RuntimeError("Could not retrieve audio for transcription.")
        return matches[0]
    return wav_path


def transcribe(path, language):
    model = get_whisper()
    lang = None if language == "auto" else language

    segments, info = model.transcribe(
        str(path),
        language=lang,
        beam_size=1,
        vad_filter=True,
        word_timestamps=False
    )

    output = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            output.append({
                "start": float(seg.start),
                "end": float(seg.end),
                "text": text
            })

    return output, getattr(info, "language", language)


def group_segments(segments, min_len, max_len):
    """Build candidate windows from transcript timing and sentence boundaries."""
    if not segments:
        return []

    candidates = []
    i = 0

    while i < len(segments):
        start = segments[i]["start"]
        chosen = []
        j = i

        while j < len(segments):
            chosen.append(segments[j])
            end = segments[j]["end"]
            duration = end - start

            if duration >= min_len:
                text = " ".join(s["text"] for s in chosen).strip()
                if duration <= max_len:
                    candidates.append({
                        "start": start,
                        "end": end,
                        "text": text,
                        "segments": chosen[:]
                    })
                break

            if duration > max_len:
                break
            j += 1

        if j >= len(segments):
            break

        # Move forward enough to produce overlapping alternatives.
        i = max(i + 1, j + 1)

    # If no full-duration windows were formed, try sliding transcript windows.
    if not candidates:
        for i, first in enumerate(segments):
            selected = []
            for seg in segments[i:]:
                selected.append(seg)
                duration = seg["end"] - first["start"]
                if duration >= min_len:
                    if duration <= max_len:
                        candidates.append({
                            "start": first["start"],
                            "end": seg["end"],
                            "text": " ".join(x["text"] for x in selected),
                            "segments": selected[:]
                        })
                    break
                if duration > max_len:
                    break

    return candidates


def score_candidate(candidate):
    text = candidate["text"]
    words = re.findall(r"\b[\w']+\b", text)
    duration = max(.1, candidate["end"] - candidate["start"])
    word_count = len(words)
    density = word_count / duration

    sentence_count = max(1, len(re.findall(r"[.!?]+", text)))
    average_sentence = word_count / sentence_count
    punctuation = len(re.findall(r"[!?]", text))

    # General speech-structure signals, not a hardcoded topic keyword list.
    density_score = min(1.0, density / 2.5)
    structure_score = min(1.0, sentence_count / 3)
    punctuation_score = min(1.0, punctuation / 3)
    length_score = 1.0 if 20 <= word_count <= 130 else .55

    score = (
        density_score * 35
        + structure_score * 25
        + punctuation_score * 15
        + length_score * 25
    )

    return round(score, 1)


def local_candidates(segments, min_len, max_len, count):
    windows = group_segments(segments, min_len, max_len)
    for item in windows:
        item["score"] = score_candidate(item)
        item["reason"] = "Ranked using speech density, sentence structure and pacing."

    windows.sort(key=lambda item: item["score"], reverse=True)

    selected = []
    for candidate in windows:
        overlaps = any(
            candidate["start"] < existing["end"] - 5
            and candidate["end"] > existing["start"] + 5
            for existing in selected
        )
        if not overlaps:
            selected.append(candidate)
        if len(selected) >= count:
            break

    return sorted(selected, key=lambda item: item["start"])


def gemini_candidates(segments, min_len, max_len, count):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or genai is None:
        return None

    transcript = "\n".join(
        f'{s["start"]:.2f}-{s["end"]:.2f}: {s["text"]}'
        for s in segments
    )[:28000]

    prompt = f"""
You are selecting short-form video moments from a timestamped transcript.
Return only valid JSON: an array of objects with start, end, reason.
Select up to {count} strong, self-contained moments.
Each must be between {min_len} and {max_len} seconds.
Prefer coherent ideas, complete stories, useful explanations, strong
opinions, entertaining exchanges, surprising insights, clear hooks,
or emotionally meaningful moments. Do not invent content.
Use the supplied timestamps. Avoid overlapping selections.
Transcript:
{transcript}
"""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    raw = (response.text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    data = json.loads(raw)

    result = []
    for item in data:
        start = float(item["start"])
        end = float(item["end"])
        if end <= start or end - start < min_len or end - start > max_len:
            continue

        matching = [
            seg for seg in segments
            if seg["end"] > start and seg["start"] < end
        ]
        if not matching:
            continue

        result.append({
            "start": start,
            "end": end,
            "text": " ".join(s["text"] for s in matching),
            "score": 90,
            "reason": str(item.get("reason") or "Selected by contextual analysis."),
            "segments": matching
        })

    return result[:count]


def ffmpeg_preview(source_path, output_path, start, end, ratio):
    require_media_tools()
    duration = max(.1, end - start)

    crop = {
        "9:16": "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'",
        "16:9": "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)'",
        "1:1": "crop='min(iw,ih)':'min(iw,ih)'",
        "4:5": "crop='min(iw,ih*4/5)':'min(ih,iw*5/4)'"
    }.get(ratio, "null")

    vf = "scale=720:-2"
    if crop != "null":
        vf = f"{crop},{vf}"

    run_command([
        ffmpeg_path(), "-y",
        "-ss", str(start), "-i", str(source_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "27", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path)
    ])


def download_url_section(source, output_path, start, end, height=720):
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed.")

    safe_url(source["url"])
    section = f"*{start:.3f}-{end:.3f}"

    options = {
        "format": (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        ),
        "outtmpl": str(output_path.with_suffix(".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "download_sections": [section],
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4"
    }

    with YoutubeDL(options) as ydl:
        ydl.download([source["url"]])

    if output_path.exists():
        return output_path

    candidates = list(output_path.parent.glob(output_path.stem + ".*"))
    candidates = [p for p in candidates if p.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not candidates:
        raise RuntimeError("This platform did not provide a partial preview download.")
    if candidates[0] != output_path:
        candidates[0].replace(output_path)
    return output_path


def create_preview(source, source_id, clip, clip_id, ratio):
    output = PREVIEW_DIR / f"{clip_id}.mp4"

    if source["kind"] == "upload":
        ffmpeg_preview(
            source["path"], output,
            clip["start"], clip["end"], ratio
        )
    else:
        download_url_section(
            source, output,
            clip["start"], clip["end"], 720
        )

    return output


def update_job(job_id, **values):
    with lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def analyze_worker(job_id, payload):
    try:
        source_id = payload["source_id"]
        source = sources[source_id]

        update_job(job_id, status="analyzing", progress=5, message="Preparing audio for transcription...")

        if source["kind"] == "upload":
            audio_path = source["path"]
        else:
            audio_path = download_audio(source, source_id)

        update_job(job_id, progress=18, message="Transcribing speech locally...")
        segments, detected_language = transcribe(audio_path, payload["language"])

        if not segments:
            raise RuntimeError("No usable speech was detected in this video.")

        update_job(job_id, progress=52, message="Finding coherent moments...")
        min_len = payload["min_duration"]
        max_len = payload["max_duration"]
        count = payload["count"]

        selected = None
        if payload.get("use_gemini"):
            try:
                selected = gemini_candidates(segments, min_len, max_len, count)
            except Exception as exc:
                update_job(
                    job_id,
                    message=f"Gemini unavailable; continuing locally. {str(exc)[:180]}"
                )

        if not selected:
            selected = local_candidates(segments, min_len, max_len, count)

        if not selected:
            raise RuntimeError(
                "Could not find enough suitable speech segments in the selected duration range."
            )

        # Exactly the requested number where enough candidates exist.
        selected = selected[:count]
        update_job(job_id, progress=65, message="Preparing individual preview segments...")

        ready = []
        for index, item in enumerate(selected):
            clip_id = uuid.uuid4().hex
            try:
                preview = create_preview(
                    source, source_id, item, clip_id, payload["ratio"]
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not prepare preview {index + 1}. "
                    f"Partial download may not be supported by this source. Details: {exc}"
                )

            clip = {
                "id": clip_id,
                "job_id": job_id,
                "source_id": source_id,
                "start": item["start"],
                "end": item["end"],
                "text": item["text"],
                "score": item["score"],
                "reason": item["reason"],
                "preview_path": str(preview),
                "preview_url": f"/preview/{preview.name}",
                "approved": False,
                "rejected": False,
                "segments": item.get("segments", []),
                "settings": payload
            }
            clips[clip_id] = clip
            ready.append({
                k: clip[k] for k in (
                    "id", "start", "end", "text", "score",
                    "reason", "preview_url", "approved", "segments"
                )
            })

            update_job(
                job_id,
                progress=65 + int(30 * (index + 1) / len(selected)),
                message=f"Preparing preview {index + 1} of {len(selected)}..."
            )

        update_job(
            job_id,
            status="preview_ready",
            progress=100,
            message=f"{len(ready)} preview clips are ready. Review and approve them.",
            clips=ready,
            detected_language=detected_language
        )

    except Exception as exc:
        update_job(
            job_id,
            status="error",
            progress=100,
            error=str(exc),
            message="Analysis could not be completed."
        )


def choose_destination():
    """Open the native Windows folder picker on the local machine."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Choose a folder for approved clips")
        root.destroy()
        return folder
    except Exception as exc:
        raise RuntimeError(f"Could not open the Windows folder picker: {exc}")


def srt_timestamp(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    secs = (milliseconds % 60000) // 1000
    ms = milliseconds % 1000
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def make_clip_srt(clip, srt_path):
    lines = []
    entries = clip.get("segments") or []

    for segment in entries:
        start = max(float(segment["start"]), clip["start"])
        end = min(float(segment["end"]), clip["end"])
        text = str(segment.get("text", "")).strip()

        if end <= start or not text:
            continue

        relative_start = start - clip["start"]
        relative_end = end - clip["start"]

        lines.extend([
            str(len(lines) // 4 + 1),
            f"{srt_timestamp(relative_start)} --> {srt_timestamp(relative_end)}",
            text.replace("-->", "—"),
            ""
        ])

    if not lines and clip.get("text"):
        duration = clip["end"] - clip["start"]
        lines = [
            "1",
            f"00:00:00,000 --> {srt_timestamp(duration)}",
            clip["text"].replace("-->", "—"),
            ""
        ]

    srt_path.write_text("\n".join(lines), encoding="utf-8-sig")


def escape_filter_path(path):
    value = Path(path).resolve().as_posix()
    return value.replace(":", r"\:").replace("'", r"\'")


def export_one(clip, destination, quality):
    require_media_tools()

    source = sources[clip["source_id"]]
    settings = clip["settings"]
    height = int(quality)

    clip_id = uuid.uuid4().hex
    raw_path = TEMP_DIR / f"{clip_id}_source.mp4"
    output = destination / (
        f"clip_{clip['start']:.1f}_{clip['end']:.1f}_{clip_id[:5]}.mp4"
    )

    start = float(clip["start"])
    end = float(clip["end"])
    duration = end - start

    # Retrieve only this selected time range for URL sources.
    if source["kind"] == "url":
        download_url_section(
            source, raw_path, start, end, height
        )
    else:
        run_command([
            ffmpeg_path(), "-y",
            "-ss", str(start),
            "-i", source["path"],
            "-t", str(duration),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c", "copy",
            str(raw_path)
        ])

    ratio = settings["ratio"]
    crop_filters = {
        "9:16": "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'",
        "16:9": "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)'",
        "1:1": "crop='min(iw,ih)':'min(iw,ih)'",
        "4:5": "crop='min(iw,ih*4/5)':'min(ih,iw*5/4)'"
    }

    filters = [crop_filters[ratio], f"scale=-2:{height}"]

    srt_path = TEMP_DIR / f"{clip_id}.srt"

    if settings.get("captions"):
        make_clip_srt(clip, srt_path)

        size_map = {"small": 18, "medium": 25, "large": 32}
        font_size = size_map.get(settings.get("caption_size"), 25)

        bg = settings.get("caption_background", "black@0.72")
        background_map = {
            "black@0.72": "&H47000000",
            "black@1": "&H00000000",
            "white@0.88": "&H1FFFFFFF"
        }
        back_colour = background_map.get(bg, "&H47000000")

        subtitle_filter = (
            f"subtitles=filename='{escape_filter_path(srt_path)}'"
            f":force_style='FontName=Arial,FontSize={font_size},"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
            f"BackColour={back_colour},BorderStyle=3,Outline=0,"
            f"Shadow=0,Alignment=2,MarginV=45'"
        )
        filters.append(subtitle_filter)

    try:
        run_command([
            ffmpeg_path(), "-y",
            "-i", str(raw_path),
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(output)
        ])
    finally:
        raw_path.unlink(missing_ok=True)
        srt_path.unlink(missing_ok=True)

    size = output.stat().st_size
    return {
        "name": output.name,
        "path": str(output),
        "size_mb": round(size / (1024 * 1024), 2)
    }

def export_worker(job_id, approved_ids, destination, quality):
    try:
        exported = []
        for index, clip_id in enumerate(approved_ids):
            clip = clips.get(clip_id)
            if not clip or not clip.get("approved"):
                continue

            result = export_one(clip, destination, quality)
            exported.append(result)

            update_job(
                job_id,
                status="exporting",
                progress=int((index + 1) / len(approved_ids) * 100),
                message=f"Exported {index + 1} of {len(approved_ids)} approved clips."
            )

        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"Exported {len(exported)} approved clips.",
            files=exported
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            progress=100,
            error=str(exc),
            message="Export failed."
        )


@app.get("/")
def index():
    return app.send_static_file("index.html") if (BASE_DIR / "static" / "index.html").exists() else (
        __import__("flask").render_template("index.html")
    )


@app.post("/api/metadata")
def api_metadata():
    try:
        data = request.get_json(force=True)
        return jsonify(extract_metadata(data.get("url", "").strip()))
    except Exception as exc:
        return json_error(exc)


@app.post("/api/upload")
def api_upload():
    file = request.files.get("video")
    if not file or not file.filename:
        return json_error("Choose a video file first.")

    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return json_error("This video file type is not supported.")

    source_id = uuid.uuid4().hex
    filename = secure_filename(file.filename) or f"{source_id}{extension}"
    path = UPLOAD_DIR / f"{source_id}_{filename}"
    file.save(path)

    try:
        info = probe_video(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    sources[source_id] = {
        "kind": "upload",
        "path": str(path),
        "title": filename,
        "duration": info["duration"]
    }

    return jsonify({
        "source_id": source_id,
        "title": filename,
        "filename": filename,
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "estimated_size": format_size(info["size"])
    })


@app.post("/api/analyze")
def api_analyze():
    data = request.get_json(force=True)
    source_id = data.get("source_id")

    if source_id not in sources:
        return json_error("Source not found. Inspect or upload the video again.")
    if data.get("count") in (None, ""):
        return json_error("Enter how many clips you want to generate.")
    try:
        requested_count = int(data["count"])
        if requested_count < 1 or requested_count > 15:
            return json_error("Choose between 1 and 15 clips.")
        payload = {
            "source_id": source_id,
            "count": requested_count,
            "ratio": data.get("ratio", "9:16"),
            "min_duration": max(5, int(data.get("min_duration", 30))),
            "max_duration": max(10, int(data.get("max_duration", 60))),
            "language": data.get("language", "auto"),
            "quality": data.get("quality", "1080"),
            "captions": bool(data.get("captions", True)),
            "use_gemini": bool(data.get("use_gemini", False)),
            "caption_background": data.get("caption_background", "black@0.72"),
            "caption_size": data.get("caption_size", "medium")
        }

        if payload["min_duration"] > payload["max_duration"]:
            return json_error("Minimum duration cannot exceed maximum duration.")

        if payload["ratio"] not in ("9:16", "16:9", "1:1", "4:5"):
            return json_error("Unsupported aspect ratio.")

        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Analysis queued.",
            "clips": []
        }

        threading.Thread(
            target=analyze_worker,
            args=(job_id, payload),
            daemon=True
        ).start()

        return jsonify({"job_id": job_id})

    except Exception as exc:
        return json_error(exc)


@app.get("/api/job/<job_id>")
def api_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job:
            return json_error("Job not found.", 404)
        return jsonify(job)


@app.get("/preview/<filename>")
def preview_file(filename):
    return send_from_directory(PREVIEW_DIR, filename)


@app.post("/api/clips/<clip_id>/adjust")
def api_adjust_clip(clip_id):
    clip = clips.get(clip_id)
    if not clip:
        return json_error("Clip not found.", 404)

    data = request.get_json(force=True)
    try:
        start = float(data.get("start"))
        end = float(data.get("end"))
    except (TypeError, ValueError):
        return json_error("Enter valid start and end times.")

    source = sources[clip["source_id"]]
    duration = float(source.get("duration") or 0)

    if start < 0 or end <= start:
        return json_error("End time must be greater than start time.")
    if duration and end > duration:
        return json_error("End time exceeds the source video duration.")

    settings = clip["settings"]
    min_len = settings["min_duration"]
    max_len = settings["max_duration"]
    if end - start < min_len or end - start > max_len:
        return json_error(f"Clip duration must be between {min_len} and {max_len} seconds.")

    old_preview = Path(clip["preview_path"])
    try:
        updated = dict(clip, start=start, end=end)
        new_preview = create_preview(
            source, clip["source_id"], updated,
            f"{clip_id}_adjusted", settings["ratio"]
        )

        old_preview.unlink(missing_ok=True)
        clip.update({
            "start": start,
            "end": end,
            "preview_path": str(new_preview),
            "preview_url": f"/preview/{new_preview.name}",
            "approved": False
        })
        return jsonify({
            "start": start,
            "end": end,
            "preview_url": clip["preview_url"]
        })
    except Exception as exc:
        return json_error(exc)


@app.post("/api/clips/<clip_id>/approval")
def api_approval(clip_id):
    clip = clips.get(clip_id)
    if not clip:
        return json_error("Clip not found.", 404)

    data = request.get_json(force=True)
    clip["approved"] = bool(data.get("approved", False))
    clip["rejected"] = bool(data.get("rejected", False))
    if clip["rejected"]:
        clip["approved"] = False

    return jsonify({
        "id": clip_id,
        "approved": clip["approved"],
        "rejected": clip["rejected"]
    })


@app.post("/api/export")
def api_export():
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    approved_ids = data.get("clip_ids") or []

    valid_ids = [
        clip_id for clip_id in approved_ids
        if clip_id in clips
        and clips[clip_id].get("approved")
        and not clips[clip_id].get("rejected")
        and clips[clip_id].get("job_id") == job_id
    ]

    if not valid_ids:
        return json_error("Approve at least one clip before exporting.")

    # Native folder picker is available because Flask runs on the same PC.
    try:
        destination_text = choose_destination()
    except Exception as exc:
        return json_error(exc, 500)

    if not destination_text:
        return jsonify({"message": "Export cancelled. No folder was selected."})

    destination = Path(destination_text)
    if not destination.exists() or not destination.is_dir():
        return json_error("The selected destination folder is not available.")

    quality = str(clips[valid_ids[0]]["settings"].get("quality", "1080"))
    if quality not in ("480", "720", "1080"):
        quality = "1080"

    export_job = uuid.uuid4().hex
    jobs[export_job] = {
        "job_id": export_job,
        "status": "exporting",
        "progress": 0,
        "message": "Starting export of approved clips.",
        "files": []
    }

    threading.Thread(
        target=export_worker,
        args=(export_job, valid_ids, destination, quality),
        daemon=True
    ).start()

    return jsonify({
        "job_id": export_job,
        "message": f"Exporting {len(valid_ids)} approved clips to your selected folder."
    })


# Install the bounded, AI-assisted pipeline overrides before Flask starts.
from pipeline_v2 import install as _install_pipeline_v2
_install_pipeline_v2(globals())

if __name__ == "__main__":
    require_media_tools()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)