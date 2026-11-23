"""Open the operating system folder dialog in its own desktop process."""

import json
import subprocess
import sys
from pathlib import Path


def choose_folder():
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "chaoticclips.picker"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, encoding="utf-8",
            check=True, timeout=180,
        )
        selected = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Folder selection timed out. Start export again to reopen it.") from exc
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError("The folder picker needs an interactive desktop with Python's Tk support.") from exc
    return Path(selected) if selected else None


def main():
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(parent=root, title="Choose a folder for approved clips", mustexist=True)
        print(json.dumps(selected))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
