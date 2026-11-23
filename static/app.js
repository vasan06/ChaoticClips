"use strict";

const $ = (id) => document.getElementById(id);

let sourceId = null;
let currentJob = null;
let clips = [];
let pollTimer = null;

function setStatus(message) {
  $("sourceStatus").textContent = message;
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (label) button.textContent = label;
}

function formatTime(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".source-pane").forEach((pane) => pane.classList.add("hidden"));
    $(tab.dataset.tab).classList.remove("hidden");
  });
});

$("inspectUrl").addEventListener("click", inspectUrl);

async function inspectUrl() {
  const url = $("videoUrl").value.trim();
  if (!url) return setStatus("Paste a video URL first.");

  const button = $("inspectUrl");
  setBusy(button, true, "Inspecting...");
  setStatus("Reading video information. The source video is not being downloaded yet.");

  try {
    const data = await requestJson("/api/metadata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });

    sourceId = data.source_id;
    showVideoInfo(data);
    $("analyseButton").disabled = false;
    setStatus("Video information loaded. Choose your clip settings and start analysis.");
  } catch (error) {
    setStatus(error.message);
  } finally {
    setBusy(button, false, "Inspect video");
  }
}

$("videoFile").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;

  const button = $("analyseButton");
  button.disabled = true;
  setStatus(`Uploading ${file.name}...`);

  const form = new FormData();
  form.append("video", file);

  try {
    const data = await requestJson("/api/upload", {
      method: "POST",
      body: form
    });

    sourceId = data.source_id;
    showVideoInfo(data);
    button.disabled = false;
    setStatus("Video uploaded and ready for analysis.");
  } catch (error) {
    setStatus(error.message);
  }
});

function showVideoInfo(data) {
  $("videoInfo").classList.remove("hidden");
  $("videoTitle").textContent = data.title || data.filename || "Selected video";
  $("videoMeta").textContent =
    `${formatTime(data.duration)}${data.width && data.height ? ` · ${data.width} × ${data.height}` : ""}`;
  $("videoQuality").textContent = data.estimated_size
    ? `Estimated source size: ${data.estimated_size}`
    : "Source information loaded.";

  const thumb = $("videoThumb");
  if (data.thumbnail) {
    thumb.src = data.thumbnail;
    thumb.classList.remove("hidden");
  } else {
    thumb.removeAttribute("src");
  }
}

$("analyseButton").addEventListener("click", startAnalysis);

async function startAnalysis() {
  if (!sourceId) return;

  const minDuration = Number($("minDuration").value);
  const maxDuration = Number($("maxDuration").value);
  const requestedCount = Number($("clipCount").value);
  if (!Number.isInteger(requestedCount) || requestedCount < 1 || requestedCount > 15) {
    return alert("Enter the number of clips you want (1 to 15).");
  }
  if (minDuration > maxDuration) {
    return alert("Minimum duration cannot be greater than maximum duration.");
  }

  const payload = {
    source_id: sourceId,
    count: requestedCount,
    ratio: $("aspectRatio").value,
    min_duration: minDuration,
    max_duration: maxDuration,
    language: $("language").value,
    quality: $("quality").value,
    captions: $("captions").checked,
    use_gemini: $("gemini").checked,
    caption_background: $("captionBg").value,
    caption_size: $("captionSize").value
  };

  $("progressSection").classList.remove("hidden");
  $("reviewSection").classList.add("hidden");
  $("clipList").replaceChildren();
  $("progressBar").style.width = "0%";
  $("progressPercent").textContent = "0%";
  $("progressMessage").textContent = "Starting analysis...";

  setBusy($("analyseButton"), true, "Analysis in progress...");

  try {
    const data = await requestJson("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    currentJob = data.job_id;
    pollJob();
  } catch (error) {
    $("progressMessage").textContent = error.message;
    setBusy($("analyseButton"), false, "Analyse and prepare previews");
  }
}

async function pollJob() {
  if (!currentJob) return;

  try {
    const data = await requestJson(`/api/job/${encodeURIComponent(currentJob)}`);
    const progress = Math.max(0, Math.min(100, Number(data.progress) || 0));

    $("progressBar").style.width = `${progress}%`;
    $("progressPercent").textContent = `${progress}%`;
    $("progressMessage").textContent = data.message || "Working...";

    if (data.status === "preview_ready" || data.status === "done") {
      clips = data.clips || [];
      renderClips(clips);
      $("reviewSection").classList.remove("hidden");
      $("reviewSection").scrollIntoView({ behavior: "smooth", block: "start" });
      setBusy($("analyseButton"), false, "Analyse again");
      return;
    }

    if (data.status === "error") {
      $("progressMessage").textContent = data.error || data.message || "Analysis failed.";
      setBusy($("analyseButton"), false, "Try again");
      return;
    }

    pollTimer = setTimeout(pollJob, 1500);
  } catch (error) {
    $("progressMessage").textContent = error.message;
    setBusy($("analyseButton"), false, "Try again");
  }
}

function renderClips(items) {
  const list = $("clipList");
  list.replaceChildren();

  $("reviewCount").textContent = `${items.length} preview${items.length === 1 ? "" : "s"}`;

  items.forEach((clip, index) => {
    const card = document.createElement("article");
    card.className = "clip-card";
    card.dataset.clipId = clip.id;

    const top = document.createElement("div");
    top.className = "clip-top";

    const titleWrap = document.createElement("div");
    const title = document.createElement("h3");
    title.className = "clip-title";
    title.textContent = `Clip ${index + 1}`;

    const meta = document.createElement("div");
    meta.className = "clip-meta";
    const previewSize = clip.preview_size_mb ? ` · ${clip.preview_size_mb} MB preview` : "";
    meta.textContent = `${formatTime(clip.start)} – ${formatTime(clip.end)} · ${Math.round(clip.score || 0)} signal${previewSize}`;

    titleWrap.append(title, meta);
    top.append(titleWrap);

    const body = document.createElement("div");
    body.className = "clip-body";

    const video = document.createElement("video");
    video.className = "clip-video";
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    if (clip.preview_url) video.src = clip.preview_url;

    const textPanel = document.createElement("div");
    const reason = document.createElement("p");
    reason.className = "clip-meta";
    reason.textContent = clip.reason || "Suggested highlight";

    const cuts = document.createElement("div");
    cuts.className = "cut-controls";

    const startLabel = document.createElement("label");
    startLabel.textContent = "Start time (seconds)";
    const startInput = document.createElement("input");
    startInput.type = "number";
    startInput.min = "0";
    startInput.step = "0.1";
    startInput.value = Number(clip.start).toFixed(1);

    const endLabel = document.createElement("label");
    endLabel.textContent = "End time (seconds)";
    const endInput = document.createElement("input");
    endInput.type = "number";
    endInput.min = "0";
    endInput.step = "0.1";
    endInput.value = Number(clip.end).toFixed(1);

    startLabel.append(startInput);
    endLabel.append(endInput);
    cuts.append(startLabel, endLabel);

    const adjust = document.createElement("button");
    adjust.className = "small-button";
    adjust.textContent = "Update cut";
    adjust.addEventListener("click", async () => {
      const start = Number(startInput.value);
      const end = Number(endInput.value);

      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
        return alert("Enter a valid start and end time.");
      }

      adjust.disabled = true;
      try {
        const result = await requestJson(`/api/clips/${encodeURIComponent(clip.id)}/adjust`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ start, end })
        });

        clip.start = result.start;
        clip.end = result.end;
        const previewSize = clip.preview_size_mb ? ` · ${clip.preview_size_mb} MB preview` : "";
    meta.textContent = `${formatTime(clip.start)} – ${formatTime(clip.end)} · ${Math.round(clip.score || 0)} signal${previewSize}`;
        if (result.preview_url) video.src = result.preview_url;
      } catch (error) {
        alert(error.message);
      } finally {
        adjust.disabled = false;
      }
    });

    const actions = document.createElement("div");
    actions.className = "clip-actions";

    const approve = document.createElement("button");
    approve.className = "small-button";
    approve.textContent = clip.approved ? "Approved" : "Approve clip";
    if (clip.approved) approve.classList.add("approved");

    approve.addEventListener("click", async () => {
      const next = !clip.approved;
      try {
        await requestJson(`/api/clips/${encodeURIComponent(clip.id)}/approval`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved: next })
        });
        clip.approved = next;
        approve.textContent = next ? "Approved" : "Approve clip";
        approve.classList.toggle("approved", next);
        updateApprovalSummary();
      } catch (error) {
        alert(error.message);
      }
    });

    const reject = document.createElement("button");
    reject.className = "small-button";
    reject.textContent = "Reject";
    reject.addEventListener("click", async () => {
      try {
        await requestJson(`/api/clips/${encodeURIComponent(clip.id)}/approval`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved: false, rejected: true })
        });
        clip.approved = false;
        clip.rejected = true;
        card.classList.add("hidden");
        updateApprovalSummary();
      } catch (error) {
        alert(error.message);
      }
    });

    actions.append(approve, adjust, reject);
    textPanel.append(reason, cuts, actions);
    body.append(video, textPanel);
    card.append(top, body);
    list.append(card);
  });

  updateApprovalSummary();
}

function updateApprovalSummary() {
  const approved = clips.filter((clip) => clip.approved && !clip.rejected).length;
  $("approvedSummary").textContent =
    approved ? `${approved} clip${approved === 1 ? "" : "s"} approved for export.` : "No clips approved yet.";
  $("exportButton").disabled = approved === 0;
}

$("exportButton").addEventListener("click", exportApproved);

async function exportApproved() {
  const approvedIds = clips
    .filter((clip) => clip.approved && !clip.rejected)
    .map((clip) => clip.id);

  if (!approvedIds.length) return;

  const button = $("exportButton");
  setBusy(button, true, "Choose destination...");

  try {
    const data = await requestJson("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJob,
        clip_ids: approvedIds
      })
    });

    $("exportStatus").textContent = data.message || "Export started.";
    if (data.job_id) {
      pollExport(data.job_id);
    } else if (data.files) {
      showExportComplete(data);
    }
  } catch (error) {
    $("exportStatus").textContent = error.message;
    setBusy(button, false, "Choose folder and export");
  }
}

async function pollExport(jobId) {
  try {
    const data = await requestJson(`/api/job/${encodeURIComponent(jobId)}`);

    $("exportStatus").textContent = data.message || "Exporting approved clips...";

    if (data.status === "done") {
      showExportComplete(data);
      return;
    }

    if (data.status === "error") {
      $("exportStatus").textContent = data.error || "Export failed.";
      setBusy($("exportButton"), false, "Choose folder and export");
      return;
    }

    setTimeout(() => pollExport(jobId), 1500);
  } catch (error) {
    $("exportStatus").textContent = error.message;
    setBusy($("exportButton"), false, "Choose folder and export");
  }
}

function showExportComplete(data) {
  const files = data.files || [];
  const details = files.map((file) => {
    const name = file.name || file.filename || "Exported clip";
    const size = file.size_mb ? ` (${file.size_mb} MB)` : "";
    return `${name}${size}`;
  });

  $("exportStatus").textContent = details.length
    ? `Export complete: ${details.join(", ")}`
    : (data.message || "Export complete.");

  setBusy($("exportButton"), false, "Export complete");
}

window.addEventListener("beforeunload", () => {
  if (pollTimer) clearTimeout(pollTimer);
});