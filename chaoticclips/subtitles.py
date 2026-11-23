"""Clip-relative subtitle and transcript formatting."""

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]


def _tokens(words, start, end):
    for word in words:
        a, b = float(word["start"]), float(word["end"])
        if not math.isfinite(a) or not math.isfinite(b) or b <= a:
            continue
        a, b = max(a, start) - start, min(b, end) - start
        text = " ".join(str(word["text"]).split())
        if a < b and text:
            yield a, b, text


def _width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text if not unicodedata.combining(char))


def _wrap(text, max_chars):
    lines = []
    line = ""
    for token in text.split():
        if line and _width(line + " " + token) > max_chars:
            lines.append(line)
            line = ""
        if line:
            line += " "
        for char in token:
            if _width(line + char) > max_chars:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return tuple(lines)


def cues(words, start, end, max_chars=32):
    """Group short phrases by timing, punctuation and available line width."""
    prepared = []
    phrase = ""
    first = last = 0.0
    for a, b, text in _tokens(words, start, end):
        joined = f"{phrase} {text}".strip()
        if phrase and (a - last > 0.50 or b - first > 2.7 or len(_wrap(joined, max_chars)) > 2):
            prepared.append((first, last, _wrap(phrase, max_chars)))
            phrase = ""
        if not phrase:
            first = a
        phrase = f"{phrase} {text}".strip()
        last = b
        if re.search(r"[.!?。！？…][\"'”’)]*$", text):
            prepared.append((first, last, _wrap(phrase, max_chars)))
            phrase = ""
    if phrase:
        prepared.append((first, last, _wrap(phrase, max_chars)))

    result = []
    for index, (a, b, lines) in enumerate(prepared):
        next_start = prepared[index + 1][0] if index + 1 < len(prepared) else end - start
        b = min(next_start, max(b, a + 0.55), end - start)
        if b > a:
            result.append(Cue(a, b, lines[:2]))
    return result


def _time(seconds, separator=","):
    hours, millis = divmod(round(seconds * 1000), 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{whole:02}{separator}{millis:03}"


def write_transcript(path, words, start, end, fmt):
    path = Path(path)
    if fmt == "txt":
        output = " ".join(text for _, _, text in _tokens(words, start, end)) + "\n"
    elif fmt in {"srt", "vtt"}:
        output = "WEBVTT\n\n" if fmt == "vtt" else ""
        separator = "." if fmt == "vtt" else ","
        for index, cue in enumerate(cues(words, start, end), 1):
            if fmt == "srt":
                output += f"{index}\n"
            output += f"{_time(cue.start, separator)} --> {_time(cue.end, separator)}\n"
            output += "\n".join(cue.lines).replace("-->", "→") + "\n\n"
    else:
        raise ValueError("Choose a TXT, SRT, or VTT transcript.")
    path.write_text(output, encoding="utf-8")


def _ass_color(color):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Subtitle colors must use six-digit hexadecimal values.")
    red, green, blue = color[1:3], color[3:5], color[5:7]
    return f"&H00{blue.upper()}{green.upper()}{red.upper()}"


def _ass_time(seconds):
    minutes, centis = divmod(round(seconds * 100), 6000)
    hours, minutes = divmod(minutes, 60)
    whole, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02}:{whole:02}.{centis:02}"


def write_ass(path, words, start, end, width, height, settings):
    """Write self-contained libass styles without incorporating untrusted tags."""
    alignment = {"bottom": 2, "middle": 5, "top": 8}[settings["subtitle_position"]]
    margin_x = max(2, round(width * 0.07))
    margin_y = max(3, round(height * (0.10 if alignment == 2 else 0.07)))
    scale = min(width, height) / 1080
    size = max(9, round(settings["subtitle_size"] * scale))
    columns = min(34, max(8, int((width - 2 * margin_x) / (size * 0.73))))
    foreground = _ass_color(settings["subtitle_color"])
    background = _ass_color(settings["subtitle_background"])
    outline = max(1, round(size / 7))
    output = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{size},{foreground},{foreground},{background},{background},"
        f"-1,0,0,0,100,100,0,0,3,{outline},0,{alignment},{margin_x},{margin_x},{margin_y},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues(words, start, end, columns):
        lines = [
            line.replace("{", "｛").replace("}", "｝").replace("\\", "＼")
            for line in cue.lines
        ]
        text = "\\N".join(lines)
        output.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(output) + "\n", encoding="utf-8")
