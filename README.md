# ChaoticClips local fork

Fresh fork of MIT-licensed [Chopify](https://github.com/mehbul/chopify), pinned in `engine/chopify/UPSTREAM.json`. The upstream copyright and license are retained. The prior implementation is preserved in Git stash and `.rebuild-backup`.

## Run from Git Bash

```bash
cd /d/viral_clip_studio
source .venv/Scripts/activate
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000. Use `python app.py --port 5000` for port 5000.

1. Paste a YouTube URL or choose a local video inside this project directory.
2. Find clips. The default disclosed reliability mode downloads the full source once and retains it in the project. Clear the cache-source checkbox to use audio-first section-only retrieval. Available source captions are reused; otherwise multilingual Whisper Tiny runs locally. Successful stages are cached.
3. Select candidates, edit timestamps and captions, then choose Preview selected. Watch the native MP4 drafts.
4. Keep the desired clips selected and choose Export selected. A native folder picker opens on this computer. Choose a writable folder inside D:\viral_clip_studio, outside `workspace/fresh/projects`. Cancellation prevents the export from starting.
5. Final MP4s, SRTs, posters, and metadata are saved to that destination. Project copies remain available through Studio.

Full-frame contain is the default for 9:16. It preserves the picture with padding. Drafts are 720×1280 and final vertical output is 1080×1920. These canvas dimensions do not increase source detail.

No account, paid API, trial, credit balance, or per-video usage charge is required. Model downloads need internet on first use. Optional reranking uses a locally installed Ollama model. Model and file processing still need CPU, RAM, disk, and time. YouTube can restrict section retrieval. The source-cache choice is displayed before processing. Section-only mode reports retrieval failures without silently switching to a full download.

## Development procedure

The licensed upstream engine, dashboard, and regression tests are under `engine/chopify`. Read the upstream [workflow](https://github.com/mehbul/chopify/blob/main/docs/WORKFLOWS.md) and [contribution procedure](https://github.com/mehbul/chopify/blob/main/CONTRIBUTING.md). Local policy changes are described in `UPSTREAM.json`. Keep caches, credentials, media, and backups outside Git. Retain the MIT license when modifying or distributing this fork.

Use the project virtual environment, pytest, and `--basetemp` inside `workspace/fresh`. All generated data stays in the project. Focused test results and current limits are recorded below.

## Current differences from upstream

- The native folder picker gates final exports. Current previews and selections are required before export.
- Only public YouTube URLs and local project videos are accepted. Local media serving is restricted.
- A complete source cache is the disclosed default, approved during this rebuild. Section-only retrieval remains available. Format detection preserves user framing and duration choices.
- The local CPU default is multilingual Whisper Tiny with bounded workers. Paid-provider and external-agent autodetection is disabled.
- Section requests use two-second padding and three-second merge gaps. Independent sections and renders use bounded concurrency.
- Campaign briefs default to keyword rules. Visual detection and semantic scoring are editing aids and require human review.
- The native local file picker reads project files in place. Browser upload copies supported videos up to 2 GiB into the local workspace.


## Verified rebuild results

- 135 focused regression tests passed, covering actual FFmpeg renders, captions, evidence analysis, ingestion, timeline scanning, upload/range HTTP routes, preview-before-export checks, destination cancellation, selected-folder output, edited-timestamp invalidation, cache reuse, background noninteractive execution, and Tamil Unicode handling.
- Real public-video section requests at 848.079–878.079 seconds timed out with the original section-only downloader. The approved full-source strategy successfully cached this same video in 71.3 seconds (approximately 203 MiB).
- A real 30.000-second 1080×1920 H.264/AAC output from that exact source interval rendered locally in 16.2 seconds with embedded Tamil source captions. Decoded output matched the corresponding source frame with mean pixel difference 1.96/255 in the image region.
- Actual browser review of the YouTube project completed three current candidate previews and confirmed native MP4 playback with readyState 4 and no media error. Previous draft variants may remain listed as cached outputs.
- The fresh app was started on port 5000 because port 8000 was already occupied. The existing listener on 8000 was left in place.

The timed download and render were separate checks. They do not establish a universal five-minute processing guarantee. The test suite exercised folder selection with controlled choices; a human-driven native folder dialog was not executed during browser verification. Source-caption word timing may be estimated within phrases. Highlight scores are heuristic editing suggestions, with a visible configurable filter (default 5/10), and need human review. New projects default to three 20–60-second candidates; the controls remain editable. The broader upstream agent/live/cloud workflows are outside this fork's verified scope. Live controls are hidden and no automatic publishing is configured.

The verified selected-source sample is `workspace/fresh/live-check/verified/verified-selected-source-moment.mp4`. Prior work is retained in the Git stash named `ChaoticClips preserved before GitHub-based fresh rebuild`, with a separate verified snapshot under `.rebuild-backup/before-fresh-20261124-201529`.
