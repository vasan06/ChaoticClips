"""Pipeline improvements for Clip Studio."""
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def install(ns):
    transcription_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe")
    ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini")
    render_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ffmpeg")
    ns["transcription_pool"] = transcription_pool
    ns["ai_pool"] = ai_pool
    ns["render_pool"] = render_pool

    def group_segments(segments, min_len, max_len):
        candidates = []
        if not segments:
            return candidates
        for i, first in enumerate(segments):
            selected = []
            for seg in segments[i:]:
                selected.append(seg)
                duration = float(seg["end"]) - float(first["start"])
                if duration > max_len:
                    break
                if duration < min_len:
                    continue
                text = " ".join(x["text"] for x in selected).strip()
                sentence_end = bool(re.search(r"[.!?][\"')\]]?$", text))
                near_target = duration >= min_len + min(8, max(2, (max_len-min_len) * .25))
                if sentence_end or near_target or seg is segments[-1]:
                    candidates.append({
                        "start": float(first["start"]), "end": float(seg["end"]),
                        "text": text, "segments": selected[:]
                    })
                    break
        return candidates

    def score_candidate(candidate):
        text = candidate["text"]
        words = re.findall(r"\b[\w']+\b", text)
        duration = max(.1, candidate["end"] - candidate["start"])
        density = len(words) / duration
        sentences = max(1, len(re.findall(r"[.!?]+", text)))
        questions = len(re.findall(r"[!?]", text))
        return round(
            35*min(1.0, density/2.4) +
            25*min(1.0, sentences/3) +
            15*min(1.0, questions/3) +
            25*(1.0 if 25 <= len(words) <= 145 else .55), 1
        )

    def local_candidates(segments, min_len, max_len, count):
        windows = group_segments(segments, min_len, max_len)
        for item in windows:
            item["score"] = score_candidate(item)
            item["reason"] = "Local fallback: ranked by speech flow and sentence boundaries."
        windows.sort(key=lambda x: x["score"], reverse=True)
        selected = []
        for item in windows:
            if any(item["start"] < x["end"]-8 and item["end"] > x["start"]+8
                   for x in selected):
                continue
            selected.append(item)
            if len(selected) >= count:
                break
        return sorted(selected, key=lambda x: x["start"])

    def gemini_video_candidates(source, min_len, max_len, count):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or ns.get("genai") is None:
            return None
        from google.genai import types
        client = ns["genai"].Client(api_key=api_key)
        if source["kind"] == "url":
            video_part = types.Part.from_uri(file_uri=source["url"], mime_type="video/*")
        else:
            uploaded = client.files.upload(file=source["path"])
            state = str(getattr(uploaded, "state", "")).upper()
            deadline = time.time() + 240
            while state and "ACTIVE" not in state and "FAILED" not in state:
                if time.time() > deadline:
                    raise RuntimeError("Gemini video upload timed out.")
                time.sleep(2)
                uploaded = client.files.get(name=uploaded.name)
                state = str(getattr(uploaded, "state", "")).upper()
            if "FAILED" in state:
                raise RuntimeError("Gemini could not process the uploaded video.")
            video_part = types.Part.from_uri(
                file_uri=uploaded.uri,
                mime_type=getattr(uploaded, "mime_type", None) or "video/mp4"
            )

        duration = float(source.get("duration") or 0)
        prompt = f"""
You are an expert short-form video editor. Inspect the actual video: speech,
visuals, pacing, reactions, demonstrations and context.
Find up to {count} genuinely engaging, self-contained moments for Shorts/Reels.
Each MUST last {min_len} to {max_len} seconds.
Return only JSON in this exact shape:
{{"clips":[{{"start":12.4,"end":52.8,"hook":"short hook/title","reason":"why this moment holds attention"}}]}}
Timestamps are absolute seconds from the beginning of the original video.
Video duration: {duration:.2f} seconds.
Prioritize a strong opening hook with payoff, surprising insight, funny or
emotional beat, compelling story, useful advice, action sequence, or memorable
exchange. Start shortly before the hook and include its payoff; do not cut
mid-sentence or before the thought resolves. Avoid introductions, dead air,
generic greetings, sponsor reads and random intervals. Avoid overlap.
Do not invent timestamps or events. Return fewer clips rather than weak clips.
"""
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=[types.Content(parts=[video_part, types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.2
            )
        )
        raw = (response.text or "").strip()
        fence = chr(96) * 3
        if raw.startswith(fence):
            raw = raw.split("\n", 1)[-1].rsplit(fence, 1)[0].strip()
        data = json.loads(raw)
        items = data.get("clips", []) if isinstance(data, dict) else data
        result = []
        for item in items:
            start, end = max(0.0, float(item["start"])), float(item["end"])
            if end <= start or end-start < min_len or end-start > max_len:
                continue
            if duration and end > duration:
                continue
            if any(start < x["end"]-5 and end > x["start"]+5 for x in result):
                continue
            result.append({
                "start": start, "end": end, "text": str(item.get("hook") or ""),
                "score": 95,
                "reason": str(item.get("reason") or "Selected by Gemini video understanding."),
                "segments": []
            })
            if len(result) >= count:
                break
        return sorted(result, key=lambda x: x["start"])


    def extract_audio(source, source_id):
        if source["kind"] == "url":
            return ns["download_audio"](source, source_id)
        audio_path = ns["AUDIO_DIR"] / f"{source_id}_16k.wav"
        ns["run_command"]([
            ns["ffmpeg_path"](), "-y", "-i", source["path"],
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(audio_path)
        ])
        return audio_path

    def render_segment(input_path, output_path, seek_start, duration,
                       ratio, height, settings=None, clip=None):
        ns["require_media_tools"]()
        crop_filters = {
            "9:16": "crop='trunc(min(iw,ih*9/16)/2)*2':'trunc(min(ih,iw*16/9)/2)*2'",
            "16:9": "crop='trunc(min(iw,ih*16/9)/2)*2':'trunc(min(ih,iw*9/16)/2)*2'",
            "1:1": "crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2'",
            "4:5": "crop='trunc(min(iw,ih*4/5)/2)*2':'trunc(min(ih,iw*5/4)/2)*2'"
        }
        filters = [crop_filters.get(ratio, crop_filters["9:16"]), f"scale=-2:{int(height)}"]
        srt_path = None
        if settings and settings.get("captions") and clip and clip.get("segments"):
            srt_path = ns["TEMP_DIR"] / f"{uuid.uuid4().hex}.srt"
            ns["make_clip_srt"](clip, srt_path)
            size_map = {"small": 18, "medium": 25, "large": 32}
            font_size = size_map.get(settings.get("caption_size"), 25)
            bg_map = {
                "black@0.72": "&H47000000",
                "black@1": "&H00000000",
                "white@0.88": "&H1FFFFFFF"
            }
            back_colour = bg_map.get(settings.get("caption_background"), "&H47000000")
            filters.append(
                f"subtitles=filename='{ns['escape_filter_path'](srt_path)}':"
                f"force_style='FontName=Arial,FontSize={font_size},"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
                f"BackColour={back_colour},BorderStyle=3,Outline=0,"
                "Shadow=0,Alignment=2,MarginV=45'"
            )
        args = [ns["ffmpeg_path"](), "-y"]
        if seek_start > 0:
            args += ["-ss", str(seek_start)]
        args += [
            "-i", str(input_path), "-t", str(max(.1, duration)),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-threads", "2", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(output_path)
        ]
        try:
            ns["run_command"](args)
        finally:
            if srt_path:
                srt_path.unlink(missing_ok=True)
        return output_path

    def ffmpeg_preview(source_path, output_path, start, end, ratio,
                       settings=None, clip=None):
        return render_segment(
            source_path, output_path, start, end-start, ratio, 720,
            settings=settings, clip=clip
        )

    def download_url_section(source, output_path, start, end, height=1080):
        if ns.get("YoutubeDL") is None:
            raise RuntimeError("yt-dlp is not installed.")
        from yt_dlp.utils import download_range_func
        ns["safe_url"](source["url"])
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        for stale in output_path.parent.glob(output_path.stem + ".*"):
            stale.unlink(missing_ok=True)
        if height in (None, "best", "source"):
            fmt = "bestvideo*+bestaudio/best"
        else:
            h = int(height)
            fmt = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        options = {
            "format": fmt,
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "download_ranges": download_range_func(None, [(float(start), float(end))]),
            "force_keyframes_at_cuts": True,
            "merge_output_format": "mp4",
            "overwrites": True
        }
        with ns["YoutubeDL"](options) as ydl:
            ydl.download([source["url"]])
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        candidates = [
            p for p in output_path.parent.glob(output_path.stem + ".*")
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
            and p.stat().st_size > 0
        ]
        if not candidates:
            raise RuntimeError("The source did not return a cut segment. Check partial-download support.")
        chosen = max(candidates, key=lambda p: p.stat().st_mtime)
        if chosen != output_path:
            chosen.replace(output_path)
        return output_path

    def create_preview(source, source_id, clip, clip_id, ratio):
        output = ns["PREVIEW_DIR"] / f"{clip_id}.mp4"
        if source["kind"] == "upload":
            ffmpeg_preview(
                source["path"], output, clip["start"], clip["end"], ratio,
                settings=clip.get("settings"), clip=clip
            )
        else:
            raw = ns["TEMP_DIR"] / f"{clip_id}_preview_source.mp4"
            try:
                download_url_section(source, raw, clip["start"], clip["end"], 1080)
                render_segment(
                    raw, output, 0, clip["end"]-clip["start"], ratio, 720,
                    settings=clip.get("settings"), clip=clip
                )
            finally:
                raw.unlink(missing_ok=True)
        return output


    def analyze_worker(job_id, payload):
        try:
            source_id = payload["source_id"]
            source = ns["sources"][source_id]
            ns["update_job"](job_id, status="analyzing", progress=4,
                             message="Preparing audio and visual analysis tasks...")
            audio_path = extract_audio(source, source_id)
            transcript_future = transcription_pool.submit(
                ns["transcribe"], audio_path, payload["language"]
            )
            ai_future = None
            if payload.get("use_gemini"):
                ai_future = ai_pool.submit(
                    gemini_video_candidates, source,
                    payload["min_duration"], payload["max_duration"], payload["count"]
                )
            ns["update_job"](job_id, progress=20,
                             message="Transcribing speech while AI reviews the video...")
            segments, detected_language = transcript_future.result()
            segments = segments or []

            selected = None
            if ai_future:
                try:
                    selected = ai_future.result()
                except Exception as exc:
                    ns["update_job"](job_id, message=f"Gemini unavailable; using fallback. {str(exc)[:140]}")
            if not selected and payload.get("use_gemini") and segments:
                try:
                    selected = ns["gemini_candidates"](
                        segments, payload["min_duration"],
                        payload["max_duration"], payload["count"]
                    )
                except Exception as exc:
                    ns["update_job"](job_id, message=f"Gemini transcript fallback unavailable: {str(exc)[:120]}")
            if not selected:
                selected = local_candidates(
                    segments, payload["min_duration"],
                    payload["max_duration"], payload["count"]
                )
            if not selected:
                raise RuntimeError(
                    "No suitable moments were found. Try shorter clip durations or enable Gemini video analysis."
                )

            selected = selected[:payload["count"]]
            ns["update_job"](job_id, progress=60,
                             message=f"Rendering {len(selected)} separate preview clips...")
            futures = {}
            for index, item in enumerate(selected):
                clip_id = uuid.uuid4().hex
                clip = {
                    "id": clip_id, "job_id": job_id, "source_id": source_id,
                    "start": item["start"], "end": item["end"],
                    "text": item.get("text", ""), "score": item.get("score", 0),
                    "reason": item.get("reason", "Suggested highlight"),
                    "approved": False, "rejected": False,
                    "segments": [
                        s for s in segments
                        if s["end"] > item["start"] and s["start"] < item["end"]
                    ],
                    "settings": payload
                }
                futures[render_pool.submit(
                    create_preview, source, source_id, clip, clip_id, payload["ratio"]
                )] = (index, clip)

            ready_by_index = {}
            completed = 0
            for future in as_completed(futures):
                index, clip = futures[future]
                preview = future.result()
                clip["preview_path"] = str(preview)
                clip["preview_url"] = f"/preview/{preview.name}"
                clip["preview_size_mb"] = round(preview.stat().st_size / (1024*1024), 2)
                ns["clips"][clip["id"]] = clip
                ready_by_index[index] = {
                    key: clip[key] for key in (
                        "id", "start", "end", "text", "score", "reason",
                        "preview_url", "preview_size_mb", "approved"
                    )
                }
                completed += 1
                ns["update_job"](
                    job_id, progress=60 + int(38*completed/len(selected)),
                    message=f"Prepared preview {completed} of {len(selected)}..."
                )

            ready = [ready_by_index[i] for i in sorted(ready_by_index)]
            message = f"{len(ready)} preview clips are ready."
            if len(ready) < payload["count"]:
                message += f" Only {len(ready)} suitable moments were found out of {payload['count']} requested."
            ns["update_job"](
                job_id, status="preview_ready", progress=100,
                message=message + " Review and approve them.",
                clips=ready, detected_language=detected_language
            )
        except Exception as exc:
            ns["update_job"](job_id, status="error", progress=100,
                             error=str(exc), message="Analysis could not be completed.")

    def export_one(clip, destination, quality):
        source = ns["sources"][clip["source_id"]]
        height = int(quality)
        clip_id = uuid.uuid4().hex
        raw = ns["TEMP_DIR"] / f"{clip_id}_source.mp4"
        output = Path(destination) / f"clip_{clip['start']:.1f}_{clip['end']:.1f}_{clip_id[:6]}.mp4"
        duration = float(clip["end"]) - float(clip["start"])
        try:
            if source["kind"] == "url":
                download_url_section(source, raw, clip["start"], clip["end"], height)
                render_segment(raw, output, 0, duration, clip["settings"]["ratio"],
                               height, settings=clip["settings"], clip=clip)
            else:
                ffmpeg_preview(source["path"], output, clip["start"], clip["end"],
                               clip["settings"]["ratio"], settings=clip["settings"],
                               clip=clip)
            size = output.stat().st_size
            return {"name": output.name, "path": str(output),
                    "size_mb": round(size/(1024*1024), 2)}
        finally:
            raw.unlink(missing_ok=True)

    def export_worker(job_id, approved_ids, destination, quality):
        try:
            approved = [
                ns["clips"].get(cid) for cid in approved_ids
                if ns["clips"].get(cid) and ns["clips"][cid].get("approved")
                and not ns["clips"][cid].get("rejected")
            ]
            if not approved:
                raise RuntimeError("No approved clips remain to export.")
            futures = {
                render_pool.submit(export_one, clip, destination, quality): i
                for i, clip in enumerate(approved)
            }
            results = {}
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
                completed += 1
                ns["update_job"](
                    job_id, status="exporting",
                    progress=int(completed/len(approved)*100),
                    message=f"Rendered {completed} of {len(approved)} approved clips."
                )
            files = [results[i] for i in sorted(results)]
            ns["update_job"](job_id, status="done", progress=100,
                             message=f"Exported {len(files)} approved clips.",
                             files=files)
        except Exception as exc:
            ns["update_job"](job_id, status="error", progress=100,
                             error=str(exc), message="Export failed.")

    ns.update({
        "group_segments": group_segments,
        "score_candidate": score_candidate,
        "local_candidates": local_candidates,
        "ffmpeg_preview": ffmpeg_preview,
        "download_url_section": download_url_section,
        "create_preview": create_preview,
        "analyze_worker": analyze_worker,
        "export_one": export_one,
        "export_worker": export_worker
    })
