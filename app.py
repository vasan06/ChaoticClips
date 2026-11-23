"""Loopback-only HTTP entry point for ChaoticClips."""

import argparse
import hashlib
import math
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from flask import Flask, Request, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from chaoticclips import highlights, media
from chaoticclips.picker import choose_folder
from chaoticclips.studio import (
    Busy, Conflict, IDENTIFIER, MAX_UPLOAD_BYTES, MEDIA_EXTENSIONS, Studio, identifier, public_source,
)

BASE = Path(__file__).resolve().parent
LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", re.I)
LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", re.I)
COLOR = re.compile(r"#[a-f0-9]{6}", re.I)
DEFAULTS = {
    "count": 3, "min_duration": 20, "max_duration": 60, "target_duration": None,
    "category": "auto", "ratio": "9:16", "resolution": "best", "language": "auto",
    "subtitle_language": "source", "transcript_format": "none", "embed_subtitles": True,
    "subtitle_size": 54, "subtitle_color": "#FFFFFF", "subtitle_background": "#151419",
    "subtitle_position": "bottom", "quality": "standard",
}
SETTINGS_OPTIONS = {
    "category": {"auto", "podcast", "education", "comedy", "motivation", "philosophy", "gaming", "story"},
    "ratio": {"9:16", "16:9", "1:1", "4:5"}, "resolution": {"best", "1080", "720", "480"},
    "transcript_format": {"none", "txt", "srt", "vtt"}, "subtitle_position": {"bottom", "middle", "top"},
    "quality": {"standard", "high", "fast"},
}


def payload(allowed=None):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object.")
    if allowed is not None and data.keys() - allowed:
        raise ValueError("This operation received unsupported fields.")
    return data


def number(value, low, high, label):
    if type(value) not in {int, float} or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} must be between {low} and {high}.")
    return value


def integer(value, low, high, label):
    if type(value) is not int:
        raise ValueError(f"{label} must be a whole number.")
    return number(value, low, high, label)


def validate_settings(values):
    settings = {**DEFAULTS, **values}
    integer(settings["count"], 1, 12, "Clip count")
    low = number(settings["min_duration"], 5, 180, "Minimum duration")
    high = number(settings["max_duration"], 5, 180, "Maximum duration")
    if high < low:
        raise ValueError("Maximum duration must be at least the minimum duration.")
    if settings["target_duration"] is not None:
        number(settings["target_duration"], low, high, "Exact duration")
    integer(settings["subtitle_size"], 24, 96, "Subtitle size")
    if type(settings["embed_subtitles"]) is not bool:
        raise ValueError("Embedded subtitles must be enabled or disabled.")
    for key, options in SETTINGS_OPTIONS.items():
        if not isinstance(settings[key], str) or settings[key] not in options:
            raise ValueError(f"Choose a supported {key.replace('_', ' ')}.")
    for key, default in (("language", "auto"), ("subtitle_language", "source")):
        value = settings[key]
        if not isinstance(value, str) or (value != default and not LANGUAGE.fullmatch(value)):
            raise ValueError("Use a supported language code.")
        settings[key] = value.casefold()
    for key in ("subtitle_color", "subtitle_background"):
        if not isinstance(settings[key], str) or not COLOR.fullmatch(settings[key]):
            raise ValueError("Use six-digit subtitle colors.")
    return settings


def create_app(data_dir=None, folder_picker=None):
    root = data_dir if data_dir is not None else BASE / "workspace" / "studio"
    studio = Studio(root, folder_picker or choose_folder, secrets.token_urlsafe(32))
    app = Flask(__name__)
    app.extensions["clipstudio"] = studio
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 1024**2,
        MAX_FORM_MEMORY_SIZE=256 * 1024,
        MAX_FORM_PARTS=4,
    )

    class StudioRequest(Request):
        def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
            return tempfile.SpooledTemporaryFile(max_size=512 * 1024, mode="w+b", dir=studio.tmp)

    app.request_class = StudioRequest

    @app.before_request
    def enforce_local_boundary():
        if not LOOPBACK_HOST.fullmatch(request.host):
            abort(400, "Use the application's loopback address.")
        if request.path.startswith("/api/"):
            token = request.headers.get("X-Clip-Token", "")
            if not token.isascii() or not secrets.compare_digest(token, studio.token):
                abort(403, "Refresh the studio page to start a local session.")
            if request.method != "GET" and request.mimetype != "multipart/form-data":
                if request.content_length is None or request.content_length > 16_384:
                    abort(413, "The JSON request is too large.")

    @app.after_request
    def harden_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https://i.ytimg.com https://*.ytimg.com https://img.youtube.com; "
            "media-src 'self'; frame-src https://www.youtube-nocookie.com; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Conflict)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(Busy)
    def busy(exc):
        return jsonify(error=str(exc)), 429

    @app.errorhandler(KeyError)
    def not_found(exc):
        return jsonify(error="The requested source, job or clip was not found."), 404

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = "Uploads must be 2 GiB or less." if exc.code == 413 and request.path == "/api/upload" else exc.description
        return jsonify(error=message), exc.code

    @app.get("/")
    def index():
        return render_template("index.html", api_token=studio.token)

    @app.post("/api/metadata")
    def metadata_route():
        url = media.canonical_url(payload({"url"}).get("url"))
        return accepted(studio.schedule("Queued to read YouTube metadata.",
                                        lambda update, _: studio.inspect_remote(url, update)))

    @app.post("/api/upload")
    def upload_route():
        if request.mimetype != "multipart/form-data":
            raise ValueError("Choose a local video file.")
        upload = request.files.get("file")
        filename = (upload.filename if upload else "") or ""
        filename = filename.replace("\\", "/").split("/")[-1][:255]
        extension = Path(filename).suffix.casefold()
        if extension not in MEDIA_EXTENSIONS:
            raise ValueError("Choose an MP4, MOV, MKV, WebM, AVI or M4V video.")
        path = studio.uploads / f"pending-{identifier()}{extension}"
        digest = hashlib.sha256()
        size = 0
        queued = False
        try:
            with path.open("xb") as target:
                while chunk := upload.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        abort(413)
                    digest.update(chunk)
                    target.write(chunk)
            if not size:
                raise ValueError("The selected file is empty.")
            job_id = studio.schedule("Queued to check the local video.",
                                     lambda update, _: studio.inspect_local(path, filename, digest.hexdigest(), update))
            queued = True
            return accepted(job_id)
        except OSError as exc:
            raise ValueError("Could not store the upload. Check the available workspace disk space.") from exc
        finally:
            if not queued:
                path.unlink(missing_ok=True)

    @app.post("/api/analyze")
    def analyze_route():
        data = payload(set(DEFAULTS) | {"source_id"})
        source_id = data.pop("source_id", None)
        settings = validate_settings(data)
        source = studio.get("source", source_id)
        return accepted(studio.schedule(
            "Queued for speech analysis.",
            lambda update, scratch: studio.analyze(source, settings, update, scratch),
            details={"source": public_source(source), "settings": settings, "clip_ids": []},
        ))

    @app.get("/api/job/<job_id>")
    def job_route(job_id):
        if not IDENTIFIER.fullmatch(job_id):
            abort(404)
        return jsonify(studio.read_job(job_id))

    @app.get("/preview/<revision>.mp4")
    def preview_route(revision):
        if not IDENTIFIER.fullmatch(revision):
            abort(404)
        path = studio.previews / f"{revision}.mp4"
        if not path.is_file() or path.is_symlink():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)

    @app.post("/api/clips/<clip_id>/review")
    def review_route(clip_id):
        review = payload({"review"}).get("review")
        if review not in ("approved", "rejected", "pending"):
            raise ValueError("Choose a valid review decision.")
        with studio.lock:
            clip = studio.get("clip", clip_id)
            if review == "approved" and studio.read_clip(clip_id)["preview_status"] != "ready":
                raise Conflict("Prepare a playable preview before approving.")
            if clip["preview_status"] == "pending":
                raise Conflict("Wait for preview preparation to finish.")
            clip["review"] = review
            studio.db.put("clip", clip_id, clip)
            return jsonify(clip=studio.read_clip(clip_id))

    @app.post("/api/clips/<clip_id>/preview")
    def edit_route(clip_id):
        data = payload({"start", "end"})
        with studio.lock:
            clip = studio.get("clip", clip_id)
            if clip["preview_status"] == "pending":
                raise Conflict("Wait for the current preview to finish.")
            source = studio.get("source", clip["source_id"])
            if data:
                start = round(number(data.get("start"), 0, source["duration"], "Start"), 3)
                end = min(source["duration"], round(number(data.get("end"), 0, source["duration"], "End"), 3))
                if not 5 <= round(end - start, 3) <= 180:
                    raise ValueError("Adjusted clips must last between 5 and 180 seconds.")
                stored = studio.db.get("speech", clip["speech_key"])
                if not stored:
                    raise Conflict("Speech cache expired. Analyze the source again.")
                context = highlights.clip_context(stored["words"], start, end)
                if (start, end) != (clip["start"], clip["end"]):
                    clip.update(start=start, end=end, duration=round(end - start, 3), text=context, edited=True)
            clip.update(review="pending", preview_status="pending", preview_error=None, revision=None)
            job_id = studio.schedule(
                "Queued to prepare a section preview.", lambda update, _: studio.rerender(clip, source, update),
                details={"clip_id": clip_id},
                prepare=lambda: studio.db.put("clip", clip_id, clip),
            )
        return accepted(job_id)

    @app.post("/api/export")
    def export_route():
        data = payload({"clip_ids", "allow_preview_fallback"})
        keys = data.get("clip_ids")
        allow_fallback = data.get("allow_preview_fallback", False)
        if type(allow_fallback) is not bool:
            raise ValueError("Choose whether to allow the approved preview as an export fallback.")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 12 or not all(
            isinstance(key, str) and IDENTIFIER.fullmatch(key) for key in keys
        ) or len(set(keys)) != len(keys):
            raise ValueError("Choose 1 to 12 distinct approved clips.")
        with studio.lock:
            clips = studio.approved(keys)
            return accepted(studio.schedule("Queued to open the destination picker.",
                                            lambda update, scratch: studio.export(
                                                clips, update, scratch, allow_fallback=allow_fallback)))

    return app


def accepted(job_id):
    return jsonify(job_id=job_id), 202


def main():
    parser = argparse.ArgumentParser(description="Start the local ChaoticClips server.")
    parser.add_argument("--port", type=int, default=8000, help="Loopback port (default: 8000).")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    workspace = BASE / "workspace" / "studio"
    scratch = workspace / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[key] = str(scratch)
    tempfile.tempdir = str(scratch)
    os.environ["HF_HOME"] = str(workspace / "models")
    os.environ["XDG_CACHE_HOME"] = str(workspace / "cache")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["FLASK_SKIP_DOTENV"] = "1"
    sys.dont_write_bytecode = True
    create_app(workspace).run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
