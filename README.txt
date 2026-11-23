VIRAL CLIP STUDIO — LOCAL EDITION
=================================

Designed for Windows 10, including modest 8 GB RAM PCs.

WHAT THIS VERSION DOES
- Runs a browser UI locally at http://127.0.0.1:5000
- Accepts local video uploads (MP4, MOV, MKV, WEBM, AVI, M4V)
- Optionally attempts YouTube URL import via yt-dlp for content you are authorized to download
- Uses faster-whisper's tiny CPU model for local transcription
- Scores transcript windows using transparent heuristic signals for hooks, emotional language,
  humour, questions, personal stories and explanatory phrases
- Selects non-overlapping candidate moments and renders MP4 clips with FFmpeg
- Supports 9:16, 16:9, 1:1 and 4:5; exports SRT subtitle files and can burn captions in

IMPORTANT LIMITATIONS
- This is a functional starter/MVP, not a clone of Vizard's proprietary models.
- The highlight scorer uses transcript heuristics, not a trained viral prediction model.
  It cannot guarantee that clips will go viral or always understand sarcasm, context or hype.
- For 8 GB RAM, the app uses the small Whisper tiny model on CPU. Processing may be slow.
- The first model run may download model weights from Hugging Face. After the model is cached,
  transcription can run locally. The initial Python package installation needs internet.
- FFmpeg and FFprobe must be installed separately and added to Windows PATH.
- The app binds only to 127.0.0.1 and does not expose the server to your network.
- YouTube URL downloading may be unavailable, blocked, or restricted. No login/cookie bypass is
  implemented. Respect YouTube terms, creator permissions, copyright and local law.
- Do not use this tool to download or republish videos without rights or permission.

INSTALL
1. Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add Python to PATH".
2. Install FFmpeg for Windows and add its bin folder to PATH. Confirm in Command Prompt:
       ffmpeg -version
       ffprobe -version
3. Extract this ZIP to a normal folder (not inside Program Files).
4. Double-click run.bat. First run installs Python dependencies and can take several minutes.
5. Open http://127.0.0.1:5000 in your browser.
6. Upload a video you have rights to use, set options and click Generate my clips.
7. Download MP4 and SRT files from the results cards.

TROUBLESHOOTING
- "FFmpeg and FFprobe are required": install FFmpeg, add its bin folder to PATH, close/reopen
  Command Prompt, and run run.bat again.
- "Speech model not installed": check pip install output; run run.bat again with internet access.
- Slow or out of memory: close other applications and use shorter/lower-resolution source videos.
- No suitable clips: try a longer video, lower AI sensitivity, or adjust clip duration.
- YouTube URL import fails: use an authorized local video file instead.

PRIVACY
Video uploads and generated clips are stored in the local uploads/ and outputs/ folders.
They are not sent to a hosted application by this program. The first-time dependency/model
installation may contact package/model hosting services.
