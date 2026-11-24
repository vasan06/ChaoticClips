"""Desktop filesystem and YouTube boundary for the local fork."""
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs
import subprocess
import sys
import json
ROOT = Path(__file__).resolve().parents[2]
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
def local_path(value, video=False):
    raw = Path(value).expanduser()
    path = raw.resolve(strict=True)
    if not path.is_relative_to(ROOT) or any(part.startswith(".") for part in path.relative_to(ROOT).parts):
        raise ValueError("Choose a file or folder inside the project directory.")
    if any(p.is_symlink() for p in [raw, *raw.parents]):
        raise ValueError("Choose a regular local path.")
    if video and (not path.is_file() or path.suffix.lower() not in VIDEO_EXTS):
        raise ValueError("Choose a supported local video file.")
    return path

def source(value):
    if not isinstance(value, str) or len(value)>2048 or any(ord(c)<32 for c in value):
        raise ValueError("Choose a video file or YouTube URL.")
    p=urlsplit(value.strip())
    if not p.scheme or len(p.scheme)==1:
        return str(local_path(value, video=True))
    if p.scheme not in {"http", "https"} or p.hostname not in {"youtube.com","www.youtube.com","m.youtube.com","youtu.be"} or p.netloc.lower()!=p.hostname:
        raise ValueError("Enter a public YouTube video URL.")
    parts=p.path.rstrip("/").split("/")
    if p.hostname=="youtu.be" and len(parts)==2: key=parts[1]
    elif p.path=="/watch": key=parse_qs(p.query).get("v",[""])[0]
    elif len(parts)==3 and parts[1] in {"shorts","embed","live"}: key=parts[2]
    else: key=""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",key): raise ValueError("Choose one YouTube video.")
    return "https://www.youtube.com/watch?v="+key

def choose(kind):
    result=subprocess.run([sys.executable,"-B",str(Path(__file__)),kind],capture_output=True,text=True,check=True,timeout=180)
    value=json.loads(result.stdout)
    if not value: return None
    return local_path(value, video=kind=="file")

if __name__=="__main__":
    import tkinter
    from tkinter import filedialog
    window=tkinter.Tk();window.withdraw();window.attributes("-topmost",True)
    try:
        if sys.argv[1]=="file": value=filedialog.askopenfilename(parent=window,title="Choose a local video",initialdir=str(ROOT),filetypes=[("Video","*.mp4 *.mov *.mkv *.webm *.avi *.m4v")])
        else: value=filedialog.askdirectory(parent=window,title="Choose where approved clips are saved",initialdir=str(ROOT),mustexist=True)
        print(json.dumps(value))
    finally: window.destroy()
