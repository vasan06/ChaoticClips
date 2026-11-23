# ChaoticClips

ChaoticClips turns long videos into short clips with a local, speech-first analysis pipeline. Review the actual cut, adjust its timestamps, and approve each clip before exporting. It uses no API keys or paid services.

## Windows setup from Git Bash

1. Install Python 3.12 or newer with Tk, and put `ffmpeg` and `ffprobe` on PATH. Use a full FFmpeg build with libass and libx264. For YouTube, also put Node.js 22+ or Deno on PATH so yt-dlp can handle supported player challenges.
2. If `.venv` is missing, create it with `python -m venv .venv`. Activate it and install the dependencies with `python -m pip install -r requirements.txt`.
3. From the project folder, activate the environment and start the app.

   ```bash
   source .venv/Scripts/activate
   python app.py
   ```

4. Open [the local studio on port 8000](http://127.0.0.1:8000). Keep Git Bash open while using the app. If port 8000 is busy, run `python app.py --port 5000` and open [port 5000](http://127.0.0.1:5000).

YouTube and the initial dependency and speech-model downloads require internet access. Local transcription defaults to the faster multilingual Whisper Tiny model. Set `CHAOTICCLIPS_WHISPER_MODEL=base` before starting for higher recognition accuracy at the cost of more CPU time. Models are cached in `workspace/studio/models`, so local video transcription can work offline after the first download.

## Make a clip

1. Load a single YouTube link or choose a local MP4, MOV, MKV, WebM, AVI, or M4V. Local uploads can be up to 2 GiB. Sources can be up to three hours long.
2. Set the requested count, clip duration range, category, aspect ratio, and output resolution. Under **Language, subtitles, and quality**, you can choose the spoken language, configure embedded subtitles, and request a separate TXT, SRT, or VTT transcript.
3. Select **Analyze and prepare previews**. The studio fetches available YouTube captions first. If it has to transcribe, it retrieves audio only. Video transfer starts when the selected cuts are rendered as economical previews. YouTube can transfer stream headers and keyframe data around the cut as part of seeking.
4. Play each candidate as soon as its MP4 appears. Up to three clips render in parallel. For a YouTube source, **Play YouTube segment** opens the identified moment even while its MP4 is being prepared. YouTube seeks are approximate. Review the rendered MP4 before approving it. Use **Edit timestamps** to regenerate it.
5. Approve the clips you want and select **Export approved clips**. Choose a folder in the native dialog on the server’s desktop. Canceling the dialog leaves the export unstarted. Once a folder is selected and validated, the studio renders the clips, then lists the filenames and destination.

For YouTube, enable **Save the approved preview if YouTube export fails** if you want the existing lower-resolution MP4 as a backup. The export result lists the actual dimensions and reports any quality fallback.

## Behavior and limits

- The Windows folder picker needs an interactive desktop. Open the browser on the host machine and select the folder in the desktop dialog. Headless or remote-only sessions cannot choose an export folder.
- MP4 previews use a smaller output size for faster review. The selected resolution applies to exports. YouTube access, connection speed and source duration affect total processing time, especially when captions are unavailable and speech must be transcribed. A local upload can supply exportable media when YouTube playback works but section rendering fails.
- Resolution settings cap the output’s shorter edge. The center crop preserves the requested aspect ratio and uses available source detail. The studio does not upscale or automatically track speakers.
- Caption timing is word-level where the source provides alignments. Some YouTube tracks contain phrase-level timestamps, so word timing in those tracks is estimated. Subtitle language selection uses available source tracks. Translation and visual-only highlights are deferred.
- SQLite persists jobs, clip decisions, and cached analysis. Previews expire after seven days and can be regenerated while analysis is cached. Uploaded sources and approved exports are retained.
- The server binds to loopback for single-user desktop use. Uploads stay on the machine. Nothing is posted or published by the application.

## Project layout

- `app.py` starts the local Flask server.
- `chaoticclips/` contains the analysis, rendering, subtitles, and export modules.
- `templates/` and `static/` contain the browser interface.
- `workspace/studio/` holds local uploads, previews, model weights, and SQLite data.

The private pre-rebuild backup is at `.rebuild-backup/before-rebuild`.
