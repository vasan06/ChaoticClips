"""Persistent job and review state for the local studio."""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path

from . import highlights, media, speech, subtitles

LOGGER = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"[a-f0-9]{32}")
MEDIA_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
MAX_SOURCE_SECONDS = 3 * 60 * 60
MAX_UPLOAD_BYTES = 2 * 1024**3
MEDIA_TTL = 7 * 24 * 60 * 60


class Conflict(Exception):
    pass


class Busy(Exception):
    pass


def identifier():
    return uuid.uuid4().hex


def matches_language(requested, actual):
    return requested.casefold().split("-")[0] == actual.casefold().split("-")[0]


def public_source(source):
    return {key: value for key, value in source.items() if key not in {"path", "fingerprint"}}


def operational_error(exc, fallback):
    if isinstance(exc, (RuntimeError, ValueError)) and str(exc):
        return str(exc)
    LOGGER.warning("Studio operation failed (%s)", type(exc).__name__)
    return fallback


class Store:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY (kind, id)
                )
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, kind, key):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, data):
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO records VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, id) DO UPDATE SET data=excluded.data, updated=excluded.updated
            """, (kind, key, json.dumps(data, ensure_ascii=False, allow_nan=False), time.time()))

    def prune(self):
        with closing(self.connect()) as db, db:
            db.execute(
                "DELETE FROM records WHERE kind='speech' AND updated<?",
                (time.time() - 30 * 24 * 60 * 60,),
            )

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute("""
                UPDATE records SET data=json_set(data, '$.status', 'error', '$.progress', 100,
                    '$.message', 'The application closed before this job finished.',
                    '$.error', 'Start the operation again.')
                WHERE kind='job' AND json_extract(data, '$.status') IN ('queued', 'running')
            """)
            db.execute("""
                UPDATE records SET data=json_set(data, '$.preview_status', 'error',
                    '$.preview_error', 'Preview was interrupted. Generate it again.')
                WHERE kind='clip' AND json_extract(data, '$.preview_status')='pending'
            """)


class Studio:
    def __init__(self, root, picker, token):
        self.root = Path(root).resolve()
        self.picker = picker
        self.token = token
        self.lock = threading.RLock()
        self.capacity = threading.BoundedSemaphore(5)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipstudio")
        self.render_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cliprender")
        self.speech_lock = threading.Lock()
        self.picker_lock = threading.Lock()
        self.tmp = self.root / "scratch"
        self.previews = self.root / "previews"
        self.uploads = self.root / "uploads"
        self.models = self.root / "models"
        for directory in (self.tmp, self.previews, self.uploads, self.models):
            directory.mkdir(parents=True, exist_ok=True)
        self.db = Store(self.root / "studio.sqlite3")
        self.db.recover()
        self.db.prune()
        self.cleanup()

    def cleanup(self):
        cutoff = time.time() - MEDIA_TTL
        for directory, pattern in ((self.previews, "*.mp4"), (self.uploads, "pending-*"),
                                   (self.tmp, "task-*"), (self.previews, ".render-*")):
            for path in directory.glob(pattern):
                try:
                    if not path.is_symlink() and path.stat().st_mtime < cutoff:
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("A transient media file could not be cleaned up.")

    def close(self):
        self.pool.shutdown(wait=True)
        self.render_pool.shutdown(wait=True)

    def schedule(self, title, action, *, details=None, prepare=None):
        if not self.capacity.acquire(blocking=False):
            raise Busy("The local job queue is full. Try again after a job finishes.")
        job_id = identifier()
        job = {
            "id": job_id, "status": "queued", "progress": 0, "message": title,
            **(details or {}),
        }
        try:
            self.db.put("job", job_id, job)
            if prepare:
                prepare()
            self.pool.submit(self.run, job_id, action)
        except Exception:
            self.capacity.release()
            raise
        return job_id

    def run(self, job_id, action):
        def update(**changes):
            self.update_job(job_id, changes)

        try:
            update(status="running")
            self.cleanup()
            with tempfile.TemporaryDirectory(dir=self.tmp, prefix="task-") as scratch:
                result = action(update, Path(scratch))
            result.setdefault("status", "done")
            update(progress=100, **result)
        except Exception as exc:
            update(status="error", progress=100, message="The operation failed.",
                   error=operational_error(exc, "The local operation could not finish."))
        finally:
            self.capacity.release()

    def update_job(self, job_id, changes):
        job = self.db.get("job", job_id)
        job.update(changes)
        self.db.put("job", job_id, job)

    def read_job(self, job_id):
        job = self.db.get("job", job_id)
        if job is None:
            raise KeyError(job_id)
        if "clip_ids" in job:
            job["clips"] = [self.read_clip(key) for key in job.pop("clip_ids")]
        if "clip_id" in job:
            job["clip"] = self.read_clip(job.pop("clip_id"))
        return {key: value for key, value in job.items() if not key.startswith("_")}

    def read_clip(self, clip_id):
        clip = self.get("clip", clip_id)
        revision = clip.get("revision")
        internal = {"revision", "source_id", "speech_key", "subtitle_key", "settings"}
        public = {key: value for key, value in clip.items() if key not in internal}
        if clip["preview_status"] == "ready":
            if revision and (self.previews / f"{revision}.mp4").is_file():
                public["preview_url"] = f"/preview/{revision}.mp4"
            else:
                public.update(preview_status="error", preview_error="Preview expired. Generate it again.")
        return public

    def get(self, kind, key):
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key):
            raise KeyError(kind)
        result = self.db.get(kind, key)
        if result is None:
            raise KeyError(kind)
        return result

    def save_source(self, source):
        if not 0 < source["duration"] <= MAX_SOURCE_SECONDS:
            raise ValueError("Choose an available video between one frame and three hours long.")
        source["id"] = identifier()
        self.db.put("source", source["id"], source)
        return public_source(source)

    def inspect_remote(self, url, update):
        update(progress=20, message="Retrieving title, duration, formats and caption availability.")
        source = media.inspect_youtube(url)
        source["fingerprint"] = url
        return {"source": self.save_source(source), "message": "YouTube metadata ready."}

    def inspect_local(self, pending, filename, digest, update):
        original = pending.with_name(f"{identifier()}{pending.suffix}")
        committed = False
        try:
            update(progress=30, message="Checking the uploaded video.")
            source = media.inspect_upload(pending)
            pending.rename(original)
            source.update(path=str(original), title=filename, fingerprint=digest)
            result = {"source": self.save_source(source), "message": "Local video ready."}
            committed = True
            return result
        finally:
            pending.unlink(missing_ok=True)
            if not committed:
                original.unlink(missing_ok=True)

    def speech_key(self, source, language, model=None):
        model = model or speech.selected_model()
        return hashlib.sha256(f"v2\0{source['fingerprint']}\0{language}\0{model}".encode()).hexdigest()

    def source_speech(self, source, language, update, scratch):
        cache_key = self.speech_key(source, language)
        cached = self.db.get("speech", cache_key)
        if cached:
            return cached, cache_key, True
        if not os.getenv("CHAOTICCLIPS_WHISPER_MODEL") and speech.selected_model() != "base":
            previous_key = self.speech_key(source, language, "base")
            previous = self.db.get("speech", previous_key)
            if previous:
                return previous, previous_key, True
        captions = None
        notes = []
        if source["kind"] == "youtube":
            update(progress=10, message="Retrieving timestamped captions when available.")
            try:
                captions = media.read_captions(source, language)
            except RuntimeError:
                notes.append("The source captions could not be retrieved. Used local transcription.")
        if captions and captions[0]:
            words, detected = captions
            method, energy = "captions", None
        else:
            update(progress=15, message="Preparing audio only for local transcription.")
            audio = media.analysis_audio(source, scratch)
            energy = speech.audio_energy(audio)
            update(progress=25, message="Transcribing the full audio locally. First use may fetch model weights.")
            last = 25

            def transcription_progress(fraction):
                nonlocal last
                value = 25 + min(24, int(fraction * 24))
                if value > last:
                    last = value
                    update(progress=value)

            words, detected = speech.transcribe(audio, language.split("-")[0], self.models, transcription_progress)
            method = "transcription"
        data = {"words": words, "language": detected, "method": method, "energy": energy, "notes": notes}
        self.db.put("speech", cache_key, data)
        return data, cache_key, False

    def subtitle_speech(self, source, data, settings):
        requested = settings["subtitle_language"]
        if not settings["embed_subtitles"] and settings["transcript_format"] == "none":
            return []
        if requested == "source" or matches_language(requested, data["language"]):
            return data["words"]
        if source["kind"] == "youtube" and any(
            matches_language(requested, lang) for lang in source["caption_languages"]
        ):
            captions = media.read_captions(source, requested)
            if captions and captions[0] and matches_language(requested, captions[1]):
                return captions[0]
        raise ValueError("The selected subtitle language has no usable source track. Choose Source language.")

    def analyze(self, source, settings, update, scratch):
        update(progress=5, message="Waiting for a speech-analysis slot.")
        with self.speech_lock:
            data, cache_key, cached = self.source_speech(source, settings["language"], update, scratch)
        update(progress=52, message="Ranking complete thoughts across the source timeline.")
        candidates = highlights.select_highlights(data["words"], source["duration"], settings, data["energy"])
        clip_ids = []
        notes = list(data["notes"])
        if len(candidates) < settings["count"]:
            notes.append(f"Found {len(candidates)} suitable moments out of {settings['count']} requested.")
        update(clip_ids=clip_ids, analysis_method=data["method"], detected_language=data["language"],
               notes=notes, cached=cached, _speech_key=cache_key)
        if not candidates:
            return {"message": "No complete speech highlights fit these settings."}

        transcript = self.subtitle_speech(source, data, settings)
        subtitle_key = identifier()
        self.db.put("speech", subtitle_key, {"words": transcript} if transcript is not data["words"] else
                    {"speech_ref": cache_key})
        clips = []
        for item in candidates:
            clip_id = identifier()
            clip = {**item, "id": clip_id, "source_id": source["id"],
                    "speech_key": cache_key, "subtitle_key": subtitle_key,
                    "settings": settings, "review": "pending", "preview_status": "pending"}
            self.db.put("clip", clip_id, clip)
            clip_ids.append(clip_id)
            clips.append(clip)
        count = len(clips)
        update(clip_ids=clip_ids, _subtitle_key=subtitle_key, progress=55,
               message=f"Preparing {count} clip previews in parallel.")
        deadline = time.monotonic() + 120
        jobs = [self.render_pool.submit(self.make_preview, clip, source, transcript, deadline=deadline)
                for clip in clips]
        ready = 0
        for completed, future in enumerate(as_completed(jobs), 1):
            ready += future.result()
            update(progress=55 + int(44 * completed / count),
                   message=f"Processed {completed} of {count} previews. {ready} playable MP4s.")
        return {"message": f"{ready} MP4 previews ready out of {count} suggested highlights."}

    def clip_words(self, clip):
        stored = self.db.get("speech", clip["subtitle_key"])
        if not stored:
            raise RuntimeError("Subtitle cache expired. Analyze the source again.")
        if "speech_ref" in stored:
            stored = self.db.get("speech", stored["speech_ref"])
        if not stored:
            raise RuntimeError("Speech cache expired. Analyze the source again.")
        return stored["words"]

    def make_preview(self, clip, source, words, *, deadline=None):
        revision = identifier()
        output = self.previews / f"{revision}.mp4"
        try:
            media.render_clip(source, output, clip["start"], clip["end"],
                              clip["settings"], words, preview=True, deadline=deadline)
            clip.update(revision=revision, preview_status="ready", preview_error=None)
        except Exception as exc:
            output.unlink(missing_ok=True)
            clip.update(revision=None, preview_status="error", review="pending",
                        preview_error=operational_error(exc, "The preview could not be prepared."))
        self.db.put("clip", clip["id"], clip)
        return clip["preview_status"] == "ready"

    def rerender(self, clip, source, update):
        update(progress=25, message="Rendering the adjusted section preview.")
        try:
            words = self.clip_words(clip)
        except RuntimeError as exc:
            clip.update(preview_status="error", preview_error=str(exc))
            self.db.put("clip", clip["id"], clip)
        else:
            self.render_pool.submit(self.make_preview, clip, source, words).result()
        return {"clip_id": clip["id"],
                "message": "Preview ready." if clip["preview_status"] == "ready" else "Preview failed."}

    def approved(self, keys):
        clips = [self.get("clip", key) for key in keys]
        for clip in clips:
            if clip["review"] != "approved" or self.read_clip(clip["id"])["preview_status"] != "ready":
                raise Conflict("Only clips with an approved, playable preview can be exported.")
        return clips

    def export(self, snapshots, update, scratch, *, allow_fallback=False):
        update(progress=5, message="Choose a destination in the folder picker on this computer.")
        with self.picker_lock:
            selected = self.picker()
        if not selected:
            return {"status": "cancelled", "message": "Folder selection cancelled. No exports were started."}
        destination = writable_destination(selected, self.root)
        with self.lock:
            clips = self.approved([clip["id"] for clip in snapshots])
            if any(clip["revision"] != snap["revision"] for clip, snap in zip(clips, snapshots)):
                raise Conflict("An approved preview changed. Review it again before exporting.")
        files, failed = {}, {}
        update(progress=10, message=f"Rendering {len(clips)} approved clips.")
        jobs = {self.render_pool.submit(self.export_clip, clip, destination, scratch,
                                       allow_fallback=allow_fallback): index
                for index, clip in enumerate(clips)}
        for completed, future in enumerate(as_completed(jobs), 1):
            index = jobs[future]
            try:
                files[index] = future.result()
            except Exception as exc:
                failed[index] = {"id": clips[index]["id"], "error": operational_error(exc, "Export could not finish.")}
            update(progress=10 + int(85 * completed / len(clips)),
                   message=f"Processed {completed} of {len(clips)} approved clips.")
        result = {"files": [files[i] for i in sorted(files)], "failed": [failed[i] for i in sorted(failed)],
                  "destination": str(destination),
                  "message": f"Exported {len(files)} of {len(clips)} approved clips."}
        if not files:
            result.update(status="error", error="Every approved clip failed to export.")
        return result

    def export_clip(self, clip, destination, scratch, *, allow_fallback=False):
        source = self.get("source", clip["source_id"])
        words = self.clip_words(clip)
        start, end = clip["start"], clip["end"]
        stem = f"ChaoticClips_{int(start):06d}s_{identifier()[:12]}"
        video = scratch / f"{stem}.mp4"
        settings = clip["settings"]
        try:
            info = media.render_clip(source, video, start, end, settings, words)
        except RuntimeError:
            if not allow_fallback or source["kind"] != "youtube":
                raise
            revision = clip.get("revision", "")
            if not revision or not IDENTIFIER.fullmatch(revision):
                raise
            preview = self.previews / f"{revision}.mp4"
            if not preview.is_file() or preview.is_symlink():
                raise
            shutil.copyfile(preview, video)
            info = media.inspect_upload(video)
            if (info["video_codec"] != "h264" or info["audio_codec"] != "aac"
                    or abs(info["duration"] - (end - start)) > .09 + 2 / max(1, info["fps"])):
                raise RuntimeError("The approved preview could not be verified for export.") from None
            info["quality_note"] = "Saved the approved lower-resolution preview after YouTube export failed."
        transcript_format = settings["transcript_format"]
        artifacts = [video]
        if transcript_format != "none":
            transcript = scratch / f"{stem}.{transcript_format}"
            subtitles.write_transcript(transcript, words, start, end, transcript_format)
            artifacts.append(transcript)
        publish(artifacts, destination)
        return {"name": video.name, "duration": info["duration"], "width": info["width"],
                "height": info["height"], "quality_note": info.get("quality_note"),
                "transcript": artifacts[1].name if len(artifacts) == 2 else None}


def writable_destination(selected, data_root):
    path = Path(selected)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("Choose an existing folder on a local drive.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The chosen folder is no longer available.") from exc
    protected = {".git", ".rebuild-backup", ".venv", ".agents", ".codex", ".aws"}
    if (str(resolved).startswith(("\\\\", "//")) or not resolved.is_dir() or resolved.is_relative_to(data_root)
            or any(part.casefold() in protected for part in resolved.parts)):
        raise ValueError("Choose a folder outside the application data and protected directories.")
    try:
        with tempfile.NamedTemporaryFile(dir=resolved, prefix=".chaoticclips-check-"):
            pass
    except OSError as exc:
        raise ValueError("The chosen folder is not writable.") from exc
    return resolved


def publish(artifacts, destination):
    staged, written = [], []
    try:
        for artifact in artifacts:
            with tempfile.NamedTemporaryFile(dir=destination, prefix=".chaoticclips-part-",
                                             delete=False) as pending:
                staged.append((Path(pending.name), destination / artifact.name))
                with artifact.open("rb") as source:
                    shutil.copyfileobj(source, pending)
                pending.flush()
                os.fsync(pending.fileno())
        for pending, final in staged:
            # A hard link publishes the complete artifact without overwriting any existing file.
            # Local filesystems without hard links can use the exclusive Windows rename.
            if os.name == "nt":
                pending.rename(final)
            else:
                os.link(pending, final)
            written.append(final)
    except OSError as exc:
        for final in written:
            final.unlink(missing_ok=True)
        raise ValueError("Could not save the finished clip in the selected folder.") from exc
    finally:
        for pending, _ in staged:
            pending.unlink(missing_ok=True)
