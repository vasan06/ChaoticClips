"""Constrained YouTube inputs and frame-accurate local MP4 rendering."""

import html
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock
from urllib.error import URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .subtitles import write_ass


_YOUTUBE = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_UPLOAD_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi"
_DIRECT_FORMATS = {"mp4", "webm", "m4a"}
_MAX_DURATION = 3 * 3600
_ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5)}
_QUALITY = {"fast": ("veryfast", "23"), "standard": ("fast", "20"), "high": ("medium", "17")}
_HLS_DOWNLOADS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="youtube-segment")
_HLS_MAX_BYTES = 192 * 1024 * 1024
_SECTION_TIMEOUT = "The YouTube section download timed out."


def canonical_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 32 for char in url):
        raise ValueError("Enter a YouTube video URL.")
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or host not in _YOUTUBE or parsed.netloc.lower() != host:
            raise ValueError
        parts = parsed.path.removesuffix("/").split("/")
        if host == "youtu.be" and len(parts) == 2:
            identifier = parts[1]
        elif host != "youtu.be" and parsed.path == "/watch":
            identifiers = parse_qs(parsed.query, max_num_fields=40).get("v", [])
            identifier = identifiers[0] if len(identifiers) == 1 else ""
        elif host != "youtu.be" and len(parts) == 3 and parts[1] in {"shorts", "live", "embed"}:
            identifier = parts[2]
        else:
            identifier = ""
        if not _VIDEO_ID.fullmatch(identifier):
            raise ValueError
    except ValueError:
        raise ValueError("Enter a YouTube URL for one video.") from None
    return f"https://www.youtube.com/watch?v={identifier}"


class _QuietLogger:
    def debug(self, message):
        pass

    info = warning = error = debug


def _youtube_info(url):
    return _cached_youtube_info(canonical_url(url), int(time.monotonic() // 300))


@lru_cache(maxsize=12)
def _cached_youtube_info(url, _time_bucket):
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise RuntimeError("Install the project's yt-dlp dependency to use YouTube.") from None

    runtimes = {name: {} for name in ("deno", "node") if shutil.which(name)}
    options = {
        "allowed_extractors": ["youtube"], "noplaylist": True, "skip_download": True,
        "socket_timeout": 8, "retries": 0, "extractor_retries": 0,
        "js_runtimes": runtimes, "remote_components": set(), "logger": _QuietLogger(),
        "proxy": "", "cachedir": str(Path(__file__).resolve().parents[1] / "workspace" / "yt-dlp-cache"),
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except (DownloadError, OSError):
        raise RuntimeError("YouTube could not provide this video. Check accessibility, connectivity, and yt-dlp setup.") from None
    if not info or info.get("_type", "video") != "video" or info.get("is_live"):
        raise RuntimeError("Choose a completed, publicly accessible YouTube video.")
    duration = float(info.get("duration") or 0)
    if not 0 < duration <= _MAX_DURATION:
        raise RuntimeError("Choose a source video with a known duration of up to three hours.")
    return info


def _remote_url(url, purpose):
    try:
        if not isinstance(url, str) or any(ord(char) < 32 for char in url):
            return False
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.netloc.lower() != host:
            return False
        if purpose == "captions":
            return host in _YOUTUBE and parsed.path == "/api/timedtext"
        if host != "googlevideo.com" and not host.endswith(".googlevideo.com"):
            return False
        if purpose == "manifest":
            return parsed.path.startswith("/api/manifest/hls_playlist/") or parsed.path.endswith(".m3u8")
        return parsed.path == "/videoplayback" or parsed.path.startswith("/videoplayback/")
    except ValueError:
        return False


class _RestrictedRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, purpose):
        self.purpose = purpose

    def redirect_request(self, request, file, code, message, headers, next_url):
        if not _remote_url(next_url, self.purpose):
            raise URLError("Unapproved redirect")
        return super().redirect_request(request, file, code, message, headers, next_url)


def _open(url, purpose, headers=None, *, timeout=7):
    if not _remote_url(url, purpose):
        raise URLError("Unapproved media URL")
    opener = build_opener(ProxyHandler({}), _RestrictedRedirects(purpose))
    return opener.open(Request(url, headers=headers or {}), timeout=timeout)


def _display_thumbnail(url):
    try:
        parsed = urlsplit(url or "")
        if parsed.scheme == "https" and parsed.netloc in {"i.ytimg.com", "img.youtube.com"}:
            return url
    except ValueError:
        pass
    return None


def inspect_youtube(url):
    url = canonical_url(url)
    info = _youtube_info(url)
    heights = {
        int(item["height"]) for item in info.get("formats", [])
        if item.get("height") and item.get("vcodec") not in {None, "none"} and not item.get("has_drm")
    }
    languages = {
        language for _, language in _caption_track(info, "auto")
        if re.fullmatch(r"[a-zA-Z0-9-]{2,24}", language)
    }
    return {
        "kind": "youtube", "url": url, "title": info.get("title") or "YouTube video",
        "duration": float(info["duration"]), "thumbnail": _display_thumbnail(info.get("thumbnail")),
        "formats": [f"{height}p" for height in sorted(heights, reverse=True)],
        "caption_languages": sorted(languages),
    }


def _caption_track(info, language):
    available = info.get("language")
    preferences = [available, "en"] if language == "auto" else [language]
    tracks = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    tried = set()

    for preference in filter(None, preferences):
        for mapping in tracks:
            matching = [key for key in mapping
                        if key.casefold() == preference.casefold() or key.casefold().startswith(preference.casefold() + "-")]
            matching.sort(key=lambda key: key.casefold() != preference.casefold())
            for lang in matching:
                for track in mapping.get(lang, []):
                    url = _caption_url(track)
                    if url and url not in tried:
                        tried.add(url)
                        yield url, lang.removesuffix("-orig")
    if language == "auto":
        for mapping in tracks:
            for lang, options in mapping.items():
                for track in options:
                    url = _caption_url(track)
                    if url and url not in tried:
                        tried.add(url)
                        yield url, lang.removesuffix("-orig")


def _caption_url(track):
    url = track.get("url")
    if track.get("ext") == "json3" and _remote_url(url, "captions") and "tlang" not in parse_qs(urlsplit(url).query):
        return url
    return None


def _parse_json3(payload, duration):
    words = []
    for event in payload.get("events", []):
        fragments = [seg for seg in event.get("segs", []) if seg.get("utf8", "").strip()]
        origin = max(0, (event.get("tStartMs") or 0) / 1000)
        edge = min(duration, origin + (event.get("dDurationMs") or 0) / 1000)
        if edge <= origin or not fragments:
            continue
        offset = len(words)
        for index, fragment in enumerate(fragments):
            tokens = html.unescape(fragment["utf8"]).split()
            if not tokens:
                continue
            start = min(edge, origin + max(0, fragment.get("tOffsetMs", 0)) / 1000)
            if index + 1 < len(fragments):
                next_fragment = fragments[index + 1]
                fallback = (edge - origin) * (index + 1) / len(fragments)
                stop = min(edge, origin + next_fragment.get("tOffsetMs", fallback * 1000) / 1000)
            else:
                stop = edge
                if "tOffsetMs" in fragment or len(fragments) > 1:
                    stop = min(stop, start + max(0.45, 0.34 * len(tokens)))
            if len(fragments) > 1 and index and "tOffsetMs" not in fragment:
                start = origin + (edge - origin) * index / len(fragments)
            for order, token in enumerate(tokens):
                a = start + (stop - start) * order / len(tokens)
                b = start + (stop - start) * (order + 1) / len(tokens)
                if (b - a) > 0.001:
                    words.append({"start": a, "end": b, "text": token})
        if len(words) > offset:
            words[-1]["cue_end"] = True
    ordered = []
    for word in sorted(words, key=lambda item: item["start"]):
        if ordered and word["start"] - ordered[-1]["start"] < 0.08 and word["text"] == ordered[-1]["text"]:
            if word.get("cue_end"):
                ordered[-1]["cue_end"] = True
            continue
        if not ordered or word["start"] >= ordered[-1]["start"] + 0.008:
            ordered.append(word)
    for current, following in zip(ordered, ordered[1:]):
        current["end"] = min(current["end"], following["start"])
    return ordered


def read_captions(source, language):
    info = _youtube_info(source["url"])
    for index, (url, lang) in enumerate(_caption_track(info, language)):
        if index == 3:
            break
        try:
            with _open(url, "captions") as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                continue
            words = _parse_json3(json.loads(data), source["duration"])
            if words:
                return words, lang
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return None


def _run(tool, args, *, cwd=None, timeout=90, error):
    executable = shutil.which(tool)
    if not executable:
        raise RuntimeError(f"{tool} is required and must be available on PATH.")
    try:
        return subprocess.run(
            [executable, *args], cwd=cwd, capture_output=True, check=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{tool} ran out of time while processing this media.") from None
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError(error) from None


def _probe(path):
    path = Path(path).resolve()
    raw = _run("ffprobe", [
        "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", _UPLOAD_FORMATS,
        "-show_format", "-show_streams", "-of", "json", str(path),
    ], error="FFprobe could not read a supported video file.")
    info = json.loads(raw)
    video = next((stream for stream in info.get("streams", [])
                  if stream.get("codec_type") == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    audio = next((stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"), {})
    if not video or not video.get("width") or not video.get("height"):
        raise RuntimeError("The file does not contain a supported video track.")
    width, height = int(video["width"]), int(video["height"])
    rotation = next((side.get("rotation", 0) for side in video.get("side_data_list", [])
                     if "rotation" in side), 0)
    if int(rotation) % 180 == 90:
        width, height = height, width
    numerator, _, denominator = video.get("avg_frame_rate", "0/1").partition("/")
    fps = int(numerator) / max(1, int(denominator or "1"))
    return {
        "width": width, "height": height,
        "duration": float(info.get("format", {}).get("duration") or 0),
        "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
        "video_index": video["index"], "audio_index": audio.get("index"), "fps": fps,
    }


def inspect_upload(path):
    info = _probe(path)
    if not 0 < info["duration"] <= _MAX_DURATION:
        raise ValueError("Choose a source video with a known duration of up to three hours.")
    if max(info["width"], info["height"]) > 7680:
        raise ValueError("Video dimensions exceed the 8K processing limit.")
    return {
        **info, "kind": "upload", "formats": [f'{info["width"]} × {info["height"]}'],
        "thumbnail": None, "caption_languages": [],
    }


def _direct_formats(info):
    return [
        item for item in reversed(info.get("formats") or [])
        if not item.get("has_drm") and item.get("protocol") == "https"
        and item.get("ext") in _DIRECT_FORMATS and _remote_url(item.get("url"), "media")
    ]


def _hls_formats(info):
    return [
        item for item in reversed(info.get("formats") or [])
        if not item.get("has_drm") and item.get("protocol") in {"m3u8", "m3u8_native"}
        and _remote_url(item.get("url"), "manifest")
    ]


def _remote_headers(item):
    headers = {}
    for key, value in (item.get("http_headers") or {}).items():
        if key.lower() in {"user-agent", "referer", "origin", "accept", "accept-language"} and not ({"\r", "\n"} & set(value)):
            headers[key] = value
    return headers


def _local_input(path, start=0):
    return [
        "-threads", "2", "-protocol_whitelist", "file", "-format_whitelist", _UPLOAD_FORMATS,
        "-ss", f"{start:.6f}", "-i", str(Path(path).resolve()),
    ]


def _remote_input(item, start=None, *, deadline=None):
    headers = _remote_headers(item)
    url = item["url"]
    if start is not None:
        try:
            with _open(url, "media", {**headers, "Range": "bytes=0-0"},
                       timeout=_request_timeout(deadline)) as response:
                if response.status != 206 or not re.fullmatch(r"bytes 0-0/[1-9][0-9]*", response.headers.get("Content-Range", "")):
                    raise RuntimeError("The provider does not support an accurate byte-range request.")
                url = response.geturl()
        except OSError:
            raise RuntimeError("YouTube could not open the requested video section.") from None
    if not _remote_url(url, "media"):
        raise RuntimeError("YouTube returned an unsupported media stream.")

    import certifi

    args = [
        "-threads", "2", "-protocol_whitelist", "https,tls,tcp", "-rw_timeout", "7000000",
        "-tls_verify", "1", "-ca_file", certifi.where(),
    ]
    if headers:
        args.extend(("-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())))
    if start is not None:
        args.extend(("-seekable", "1", "-ss", f"{start:.6f}"))
    args.extend(("-i", url))
    return args


def _request_timeout(deadline):
    if deadline is None:
        return 7
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(_SECTION_TIMEOUT)
    return min(7, remaining)


def _hls_section(item, playlist, start, end, *, deadline=None):
    """Build a VOD playlist containing only segments intersecting this cut."""
    headers = _remote_headers(item)
    with _open(item["url"], "manifest", headers, timeout=_request_timeout(deadline)) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        base = response.geturl()
    if len(data) > 4 * 1024 * 1024:
        raise RuntimeError("The YouTube segment index is too large.")
    lines = data.decode("utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise RuntimeError("YouTube did not return a completed media playlist.")

    selected = []
    earliest = elapsed = 0.0
    duration = None
    sequence = 0
    init = None
    for raw in lines[1:]:
        line = raw.strip()
        if line in {"#EXT-X-DISCONTINUITY", "#EXT-X-GAP"} or line.startswith(("#EXT-X-BYTERANGE", "#EXT-X-KEY")):
            raise RuntimeError("This YouTube playlist uses an unsupported segment layout.")
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            match = re.fullmatch(r'#EXT-X-MAP:URI="([^"]+)"', line)
            if not match or selected:
                raise RuntimeError("This YouTube playlist changes its media initialization.")
            init = urljoin(base, match[1])
            if not _remote_url(init, "media") or '"' in init:
                raise RuntimeError("YouTube returned an unsupported initialization URL.")
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
            if not math.isfinite(duration) or not 0 < duration <= 60:
                raise RuntimeError("YouTube returned an invalid segment duration.")
        elif line and not line.startswith("#"):
            if duration is None:
                raise RuntimeError("YouTube returned an unsupported playlist.")
            if elapsed < end and elapsed + duration > start:
                url = urljoin(base, line)
                if not _remote_url(url, "media"):
                    raise RuntimeError("YouTube returned an unsupported segment URL.")
                if not selected:
                    earliest = elapsed
                selected.append((duration, url, sequence))
            elapsed += duration
            sequence += 1
            duration = None
            if elapsed >= end:
                break
    if not selected or elapsed + 0.15 < end:
        raise RuntimeError("The YouTube playlist does not cover this cut.")
    if deadline is not None and len(selected) == sum(line.startswith("#EXTINF:") for line in lines):
        raise RuntimeError("This cut would require the entire YouTube stream. Choose a shorter section.")

    target_duration = math.ceil(max(seconds for seconds, _, _ in selected))
    output = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target_duration}",
              f"#EXT-X-MEDIA-SEQUENCE:{selected[0][2]}"]
    if init:
        output.append(f'#EXT-X-MAP:URI="{init}"')
    for seconds, url, _ in selected:
        output.extend((f"#EXTINF:{seconds:.6f},", url))
    output.append("#EXT-X-ENDLIST")
    playlist.write_text("\n".join(output) + "\n", encoding="utf-8")
    return max(0.0, start - earliest)


def _localize_hls(playlist, headers, deadline, *, limit=_HLS_MAX_BYTES):
    """Fetch only selected segments, with a shared byte budget and bounded parallelism."""
    lines = playlist.read_text(encoding="utf-8").splitlines()
    fragmented = any(line.startswith("#EXT-X-MAP:") for line in lines)
    bytes_left = limit
    lock = Lock()
    stopped = Event()

    def fetch(url, path):
        nonlocal bytes_left
        if stopped.is_set():
            return
        try:
            with _open(url, "media", headers, timeout=_request_timeout(deadline)) as response, path.open("wb") as target:
                received = 0
                length = response.headers.get("Content-Length")
                read = getattr(response, "read1", response.read)
                while not stopped.is_set():
                    _request_timeout(deadline)
                    block = read(64 * 1024)
                    if not block:
                        break
                    _request_timeout(deadline)
                    with lock:
                        if len(block) > bytes_left:
                            raise RuntimeError("The selected YouTube section exceeded its download size limit.")
                        bytes_left -= len(block)
                    target.write(block)
                    received += len(block)
                if not stopped.is_set() and (not received or (length and length.isdecimal() and received != int(length))):
                    raise RuntimeError("A YouTube segment ended before its media finished downloading.")
        except TimeoutError:
            raise RuntimeError(_SECTION_TIMEOUT) from None
        except OSError as error:
            if isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
                raise RuntimeError(_SECTION_TIMEOUT) from None
            raise RuntimeError("A YouTube segment could not be downloaded.") from None

    downloads = []
    for index, line in enumerate(lines):
        init = line.startswith("#EXT-X-MAP:")
        if not init and (not line or line.startswith("#")):
            continue
        url = line.removeprefix('#EXT-X-MAP:URI="').removesuffix('"') if init else line
        suffix = ".mp4" if init else (".m4s" if fragmented else ".ts")
        path = playlist.with_name(f"{playlist.stem}-{index}{suffix}")
        downloads.append((url, path))
        lines[index] = f'#EXT-X-MAP:URI="{path.name}"' if init else path.name
    futures = [_HLS_DOWNLOADS.submit(fetch, url, path) for url, path in downloads]
    try:
        for future in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
            future.result()
    except TimeoutError:
        raise RuntimeError(_SECTION_TIMEOUT) from None
    finally:
        stopped.set()
        wait([future for future in futures if not future.cancel()])
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _section_input(item, scratch, start, end, name, *, deadline=None):
    if item["protocol"] == "https":
        return _remote_input(item, start, deadline=deadline)
    import certifi

    playlist = scratch / f"{name}.m3u8"
    try:
        offset = _hls_section(item, playlist, start, end, deadline=deadline)
    except TimeoutError:
        raise RuntimeError(_SECTION_TIMEOUT) from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise RuntimeError(_SECTION_TIMEOUT) from None
        raise RuntimeError("YouTube could not open the segment index.") from None
    if deadline is not None:
        _localize_hls(playlist, _remote_headers(item), deadline)
        return [
            "-threads", "2", "-protocol_whitelist", "file", "-f", "hls",
            "-allowed_extensions", "ALL", "-extension_picky", "0",
            "-ss", f"{offset:.6f}", "-i", str(playlist),
        ]
    headers = "".join(f"{key}: {value}\r\n" for key, value in _remote_headers(item).items())
    return [
        "-threads", "2", "-protocol_whitelist", "file,https,tls,tcp",
        "-rw_timeout", "7000000", "-tls_verify", "1", "-ca_file", certifi.where(),
        *(["-headers", headers] if headers else []),
        "-f", "hls", "-allowed_extensions", "ALL", "-extension_picky", "0",
        "-ss", f"{offset:.6f}", "-i", str(playlist),
    ]


def analysis_audio(source, workdir):
    workdir = Path(workdir)
    output = workdir / "analysis.wav"
    remote = source["kind"] == "youtube"
    duration = float(source["duration"])
    candidates = [None]
    deadline = time.monotonic() + 110
    if remote:
        info = _youtube_info(source["url"])
        candidates = []
        for formats in (_direct_formats(info), _hls_formats(info)):
            audio = [item for item in formats if item.get("vcodec") == "none"
                     and item.get("acodec") not in {None, "none"}]
            audio.sort(key=lambda item: abs(float(item.get("abr") or 50) - 50))
            candidates.extend(audio[:1])
        if not candidates:
            raise RuntimeError("YouTube supplied no audio-only stream for speech analysis. Try a local upload.")
    for item in candidates:
        if time.monotonic() >= deadline:
            break
        try:
            if item is None:
                args = _local_input(source["path"])
            elif item["protocol"] == "https":
                args = _remote_input(item)
            else:
                args = _section_input(item, workdir, 0, duration, "analysis")
            timeout = min(75, max(1, deadline - time.monotonic())) if remote else 180
            _run("ffmpeg", [
                "-v", "error", "-nostdin", "-y", *args, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                "-t", f"{duration:.6f}", "-c:a", "pcm_s16le", str(output.resolve()),
            ], timeout=timeout, error="Audio extraction failed. Check that the source has a playable audio track.")
            with wave.open(str(output), "rb") as audio:
                seconds = audio.getnframes() / audio.getframerate()
            if seconds and (not remote or seconds >= duration - max(2, duration * .005)):
                return output
        except (RuntimeError, OSError, ValueError, wave.Error, EOFError):
            pass
        output.unlink(missing_ok=True)
    raise RuntimeError("Audio extraction failed. Check the source audio or try a local upload.")


def _stream_choices(info, cap, *, preview=False):
    direct = _direct_formats(info)
    hls = _hls_formats(info)
    video = [item for item in direct + hls if item.get("vcodec") not in {None, "none"}
             and item.get("width") and item.get("height")
             and max(item["width"], item["height"]) <= 7680]
    audio = [item for item in direct + hls if item.get("vcodec") == "none"
             and item.get("acodec") not in {None, "none"}]
    video.sort(key=lambda item: (
        _quality_rank(item, cap, preview), bool(item.get("acodec") not in {None, "none"}),
        item.get("vcodec", "").startswith("avc"), preview and item["protocol"] != "https",
    ), reverse=True)
    choices = []
    seen = set()
    for item in video:
        muxed = item.get("acodec") not in {None, "none"}
        direct_video = item["protocol"] == "https"
        companion = None if muxed else max(
            audio, key=lambda track: ((track["protocol"] == "https") == direct_video,
                                      track.get("ext") == "m4a"), default=None
        )
        if (muxed or companion) and item["url"] not in seen:
            choices.append((item, companion))
            seen.add(item["url"])
    if choices:
        first, *remaining = choices
        yield first
        alternate = next((choice for choice in remaining
                          if (choice[0]["protocol"] == "https") != (first[0]["protocol"] == "https")), None)
        if alternate:
            yield alternate
        yield from (choice for choice in remaining if choice is not alternate)


def _quality_rank(item, cap, preview):
    size = min(item["width"], item["height"])
    if not cap:
        return size, 0
    if preview:
        return (size if size <= cap else -size), 0
    return min(size, cap), -size


def _render_choices(info, cap, preview):
    preferred = list(_stream_choices(info, cap, preview=preview))
    if preview:
        economical = list(_stream_choices(info, min(cap or 360, 240), preview=True))
        candidates = preferred[:1] + economical[:1] + preferred[1:3]
    else:
        # Reserve attempts for lighter streams after higher-resolution failures.
        moderate = list(_stream_choices(info, min(cap or 1080, 1080), preview=True))
        economical = list(_stream_choices(info, 360, preview=True))
        candidates = preferred[:2] + moderate[:1] + economical[:2]
    seen = set()
    for video, audio in candidates:
        key = (video["url"], audio["url"] if audio else None)
        if key not in seen:
            seen.add(key)
            yield video, audio


def _output_size(width, height, ratio, cap):
    if ratio == "source":
        scale = min(1, cap / min(width, height)) if cap else 1
        size = tuple(min(edge // 2, round(edge * scale / 2)) * 2 for edge in (width, height))
        if not min(size):
            raise RuntimeError("The source is too small to render.")
        return size
    horizontal, vertical = _ASPECTS[ratio]
    factor = min(width // horizontal, height // vertical)
    if cap:
        factor = min(factor, cap // min(horizontal, vertical))
    factor -= factor % 2
    if not factor:
        raise RuntimeError("The source is too small for this aspect ratio.")
    return horizontal * factor, vertical * factor


def _render(inputs, mapping, size, scratch, start, end, settings, words, preview, timeout=300):
    width, height = size
    relative = f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos,crop={width}:{height},setsar=1"
    if settings["embed_subtitles"] and words:
        write_ass(scratch / "captions.ass", words, start, end, width, height, settings)
        relative += ",ass=captions.ass"
    preset, crf = ("ultrafast", "25") if preview else _QUALITY[settings["quality"]]
    output = scratch / "clip.mp4"
    _run("ffmpeg", [
        "-v", "error", "-nostdin", "-y", "-filter_threads", "2", *inputs,
        *mapping, "-t", f"{end - start:.6f}", "-vf", relative,
        "-c:v", "libx264", "-threads", "2", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k" if preview else "160k",
        "-movflags", "+faststart", "-sn", "-dn", "-map_metadata", "-1", "clip.mp4",
    ], cwd=scratch, timeout=timeout, error="FFmpeg could not finish this section. Check media access and FFmpeg/libass support.")

    result = _probe(output)
    tolerance = 0.09 + 2 / max(1, result["fps"])
    if (abs(result["duration"] - (end - start)) > tolerance
            or (result["width"], result["height"]) != size or result["video_codec"] != "h264"):
        raise RuntimeError("The rendered clip did not match its expected duration or format.")
    if "-map" in mapping[2:] and result["audio_codec"] != "aac":
        raise RuntimeError("The rendered clip did not contain the expected AAC audio.")
    return output, result


def _section_inputs(video, audio, scratch, start, end, deadline):
    if audio is None:
        inputs = _section_input(video, scratch, start, end, "video", deadline=deadline)
        return inputs, ["-map", "0:v:0", "-map", "0:a:0"]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="youtube-track") as tracks:
        video_input = tracks.submit(_section_input, video, scratch, start, end, "video", deadline=deadline)
        audio_input = tracks.submit(_section_input, audio, scratch, start, end, "audio", deadline=deadline)
        return video_input.result() + audio_input.result(), ["-map", "0:v:0", "-map", "1:a:0"]


def render_clip(source, output, start, end, settings, subtitle_words, *, preview=False, deadline=None):
    start, end = float(start), float(end)
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= float(source["duration"])):
        raise ValueError("Clip timestamps are outside the source.")
    if end - start > 180:
        raise ValueError("Clips are limited to three minutes.")
    output = Path(output).resolve()
    resolution = settings["resolution"]
    cap = 360 if preview else None if resolution == "best" else int(resolution)
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError("Preview time limit reached. Retry this clip individually.")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".render-") as directory:
        scratch = Path(directory)
        if source["kind"] == "upload":
            local = source if "video_index" in source else inspect_upload(source["path"])
            inputs = _local_input(source["path"], start)
            mapping = ["-map", f"0:{local['video_index']}"]
            if local.get("audio_index") is not None:
                mapping.extend(("-map", f"0:{local['audio_index']}"))
            size = _output_size(local["width"], local["height"], settings["ratio"], cap)
            timeout = max(1, deadline - time.monotonic()) if deadline is not None else 300
            rendered, result = _render(inputs, mapping, size, scratch, start, end, settings, subtitle_words,
                                       preview, timeout=timeout)
            rendered.replace(output)
            return result

        if end - start >= source["duration"] - 0.05:
            raise ValueError("Select a section shorter than the full YouTube video.")
        choices = _render_choices(_youtube_info(source["url"]), cap, preview)
        render_deadline = time.monotonic() + 180
        deadline = min(deadline, render_deadline) if deadline is not None else render_deadline
        attempted = False
        failure = None
        best_attempted_pixels = 0
        for index, (video, audio) in enumerate(choices):
            if time.monotonic() >= deadline:
                break
            attempted = True
            try:
                size = _output_size(video["width"], video["height"], settings["ratio"], cap)
                pixels = size[0] * size[1]
                best_attempted_pixels = max(best_attempted_pixels, pixels)
                attempt_seconds = 85 if preview else 55
                attempt_deadline = min(deadline, time.monotonic() + attempt_seconds)
                transfer_deadline = min(attempt_deadline - 5, time.monotonic() + (65 if preview else 40))
                with tempfile.TemporaryDirectory(dir=scratch, prefix="attempt-") as attempt:
                    temporary = Path(attempt)
                    inputs, mapping = _section_inputs(video, audio, temporary, start, end, transfer_deadline)
                    timeout = max(1, attempt_deadline - time.monotonic())
                    rendered, result = _render(inputs, mapping, size, temporary, start, end, settings,
                                               subtitle_words, preview, timeout=timeout)
                    if not preview and pixels < best_attempted_pixels:
                        result["quality_note"] = "A lower-resolution stream was used after higher-resolution attempts failed."
                    rendered.replace(output)
                    return result
            except RuntimeError as error:
                failure = str(error)
            except (OSError, ValueError):
                continue
    if not attempted:
        raise RuntimeError("YouTube did not expose a supported stream. Retry this clip or upload the same full-length original.")
    guidance = "Retry this clip or upload the same full-length original."
    raise RuntimeError(f"{failure or 'YouTube media could not be rendered as an MP4.'} {guidance}")
