"""Start the licensed ChaoticClips desktop fork on loopback."""
import os
from pathlib import Path
import sys
import tempfile
BASE = Path(__file__).resolve().parent
RUNTIME = BASE / "workspace" / "fresh"
(RUNTIME / "scratch").mkdir(parents=True, exist_ok=True)
for key in ("TEMP", "TMP", "TMPDIR"):
    os.environ[key] = str(RUNTIME / "scratch")
tempfile.tempdir = str(RUNTIME / "scratch")
os.environ.update(HF_HOME=str(BASE / "workspace" / "studio" / "models"), HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_XET="1", XDG_CACHE_HOME=str(RUNTIME / "cache"), CHOPIFY_MODELS_DIR=str(RUNTIME / "models"), CHOPIFY_SOURCE_CACHE=str(RUNTIME / "source-cache"), CHOPIFY_RENDER_WORKERS="2", CHOPIFY_SECTION_WORKERS="3", CHOPIFY_ASR_PARTS="1", CHOPIFY_DISCOVERY_AUDIO="worstaudio", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
sys.dont_write_bytecode = True
sys.path.insert(0, str(BASE / "engine" / "chopify"))
if __name__ == "__main__":
    import dashboard
    if "--port" not in sys.argv:
        sys.argv += ["--port", "8000"]
    if "--projects-dir" not in sys.argv:
        sys.argv += ["--projects-dir", str(RUNTIME / "projects")]
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")
    dashboard.main()
