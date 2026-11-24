"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="clip-token"]').content;
const sessionKey = "chaoticclips.workspace";
const terminalStatuses = new Set(["done", "cancelled", "error"]);
const categories = {
  auto: "Highlight",
  podcast: "Podcast / interview",
  education: "Educational insight",
  comedy: "Comedy",
  motivation: "Motivation",
  philosophy: "Philosophy",
  gaming: "Gaming commentary",
  story: "Story",
};
const reviews = { pending: "Needs review", approved: "Approved", rejected: "Rejected" };
const state = {
  source: null,
  settings: null,
  clips: new Map(),
  sourceBusy: false,
  analysisBusy: false,
  exportBusy: false,
  pendingClips: new Set(),
  jobs: { preview: {} },
};
const sourcePlayer = {
  id: null,
  directURL: "",
  generation: 0,
  busy: false,
  offset: 0,
  windowEnd: 0,
  pendingSeek: null,
  autoplay: false,
  draft: false,
};
let lastSourceStatus = "";
let lastSourceTone = "";

class RequestError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

async function api(path, payload) {
  const headers = { "X-Clip-Token": token };
  const options = { headers, cache: "no-store", redirect: "error", mode: "same-origin" };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options).catch(() => {
    throw new RequestError("The local server could not be reached. Check that ChaoticClips is running.");
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new RequestError(data.error || "The local server could not complete this request.", response.status);
  return data;
}

function uploadFile(file, path = "/api/upload", progressId = "uploadProgress", fields = {}) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const payload = new FormData();
    payload.append("file", file);
    for (const [key, value] of Object.entries(fields)) payload.append(key, value);
    request.open("POST", path);
    request.setRequestHeader("X-Clip-Token", token);
    request.responseType = "json";
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) $(progressId).value = Math.min(100, event.loaded / event.total * 100);
    });
    request.onload = () => {
      if (request.status >= 200 && request.status < 300 && request.response) {
        resolve(request.response);
      } else {
        const message = request.status === 413 ? "Choose a video up to 2 GiB." : "The file could not be loaded.";
        reject(new RequestError(request.response?.error || message, request.status));
      }
    };
    request.onerror = () => reject(new RequestError("Upload interrupted. Check that ChaoticClips is running and try again."));
    request.send(payload);
  });
}

function setStatus(node, message, tone = "") {
  if (node.textContent !== message) node.textContent = message;
  node.classList.toggle("is-error", tone === "error");
  node.classList.toggle("is-success", tone === "success");
}

function sourceStatus(message, tone = "") {
  lastSourceStatus = message;
  lastSourceTone = tone;
  setStatus($("sourceStatus"), message, tone);
}

function busy(button, value) {
  button.setAttribute("aria-busy", String(value));
}

function limitPercent(value) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function progress(bar, label, value) {
  if (!Number.isFinite(value)) {
    bar.removeAttribute("value");
    if (label) label.textContent = "";
    return;
  }
  const percentage = limitPercent(value);
  bar.value = percentage;
  if (label) label.textContent = String(percentage) + "%";
}

function formatTime(seconds, milliseconds = false) {
  const duration = Math.max(0, Math.round(Number(seconds) * 1000));
  const hours = Math.floor(duration / 3600000);
  const minutes = Math.floor(duration / 60000) % 60;
  const wholeSeconds = Math.floor(duration / 1000) % 60;
  const pad = (number, digits = 2) => String(number).padStart(digits, "0");
  const time = (hours ? pad(hours) + ":" : "") + pad(minutes) + ":" + pad(wholeSeconds);
  return milliseconds ? time + "." + pad(duration % 1000, 3) : time;
}

function secondsLabel(value) {
  return new Intl.NumberFormat("en", { maximumFractionDigits: 2 }).format(value) + " s";
}

function saveJobs() {
  try {
    localStorage.setItem(sessionKey, JSON.stringify(state.jobs));
  } catch {
    // The active session still works when the browser blocks local storage.
  }
}

function markJob(type, id) {
  state.jobs[type] = id;
  saveJobs();
}

function clearJob(type) {
  delete state.jobs[type];
  saveJobs();
}

function reconnectButton(id, visible) {
  const button = $(id);
  button.hidden = !visible;
  button.disabled = !visible;
}

function jobURL(id) {
  return "/api/job/" + encodeURIComponent(id);
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function followJob(id, onUpdate, isCurrent = () => true) {
  let failures = 0;
  while (isCurrent()) {
    let job;
    try {
      job = await api(jobURL(id));
      failures = 0;
    } catch (error) {
      if (error.status || ++failures >= 4) throw error;
      await delay(700 * failures);
      continue;
    }
    if (!isCurrent()) return null;
    onUpdate(job);
    if (terminalStatuses.has(job.status)) return job;
    await delay(850);
  }
  return null;
}

function updateControls() {
  const workflowBusy = state.sourceBusy || state.analysisBusy || state.exportBusy || state.pendingClips.size > 0;
  for (const input of document.querySelectorAll('input[name="source_mode"], #urlForm input, #urlForm button, #uploadForm input, #uploadForm button, #recoveryForm input, #recoveryForm button')) {
    input.disabled = workflowBusy;
  }
  $("analyzeButton").disabled = !state.source || workflowBusy;
  busy($("analyzeButton"), state.analysisBusy);
  $("settingsFields").disabled = state.analysisBusy || state.exportBusy;
  $("subtitleFields").disabled = !$("embedSubtitles").checked;

  const records = [...state.clips.values()];
  const count = records.filter(({ clip }) => exportable(clip)).length;
  if (records.length) {
    const ready = records.filter(({ clip }) => clip.preview_status === "ready").length;
    setStatus($("reviewCount"), ready + " of " + records.length + " MP4 previews ready");
    setStatus($("approvedSummary"), count ? count + (count === 1 ? " clip approved" : " clips approved") : "Approve a clip after reviewing its preview.");
  }
  if (state.settings) {
    const { ratio, resolution, embed_subtitles, transcript_format } = state.settings;
    const output = [ratio, resolution === "best" ? "Best available resolution" : "Up to " + resolution + "p"];
    if (embed_subtitles) output.push("Embedded subtitles");
    if (transcript_format !== "none") output.push(transcript_format.toUpperCase() + " transcript");
    setStatus($("exportSettings"), output.join(" · "));
  }
  $("exportButton").disabled = !count || workflowBusy;
  $("exportFallbackOption").hidden = state.source?.kind !== "youtube" || Boolean(state.source.attached_title);
  $("exportFallback").disabled = workflowBusy;
  busy($("exportButton"), state.exportBusy);
  for (const record of records) updateClipActions(record);
}

function switchMode(mode) {
  $("urlForm").hidden = mode !== "url";
  $("uploadForm").hidden = mode !== "file";
  setStatus($("sourceStatus"), state.source ? lastSourceStatus : "", state.source ? lastSourceTone : "");
  $("videoUrl").removeAttribute("aria-invalid");
  $("videoFile").removeAttribute("aria-invalid");
}

function displaySource(source) {
  const previous = state.source;
  state.source = source;
  if (source.id !== previous?.id) {
    $("attachRecovery").open = false;
    $("recoveryForm").reset();
    setStatus($("recoveryStatus"), "");
  }
  $("attachRecovery").hidden = source.kind !== "youtube";
  $("attachmentName").hidden = !source.attached_title;
  setStatus($("attachmentName"), source.attached_title ? "Attached video: " + source.attached_title : "", "success");
  $("recoveryForm").hidden = Boolean(source.attached_title);
  configureSourcePlayer(source);
  $("videoTitle").textContent = source.title || "Source video";
  $("videoDuration").textContent = formatTime(source.duration);
  $("videoFormats").textContent = source.formats?.length ? source.formats.join(", ") : "Source format";
  const languages = source.caption_languages || [];
  const samples = languages.slice(0, 5).join(", ");
  $("videoCaptions").textContent = languages.length > 5
    ? samples + " and " + (languages.length - 5) + " more"
    : samples || "None detected";
  $("videoInfo").hidden = false;

  const thumb = $("videoThumb");
  $("thumbnailFrame").hidden = true;
  thumb.removeAttribute("src");
  if (source.thumbnail) {
    try {
      const url = new URL(source.thumbnail);
      if (url.protocol === "https:" && ["i.ytimg.com", "img.youtube.com"].includes(url.hostname)) {
        $("thumbnailFrame").hidden = false;
        thumb.src = url.href;
      }
    } catch {
      // A thumbnail is optional. Source details remain usable.
    }
  }

  const tracks = $("subtitleLanguage");
  const oldLanguage = tracks.value;
  tracks.replaceChildren(new Option("Use spoken language", "source"));
  const displayNames = new Intl.DisplayNames(["en"], { type: "language" });
  for (const code of new Set(languages)) {
    if (!/^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$/i.test(code)) continue;
    let name = code;
    try {
      name = displayNames.of(code) || code;
    } catch {
      // Display the code when the browser cannot provide a localized name.
    }
    tracks.add(new Option(name, code));
  }
  if ([...tracks.options].some(({ value }) => value === oldLanguage)) tracks.value = oldLanguage;
  $("languageHint").textContent = tracks.options.length > 1
    ? "Use the spoken language or an available caption track."
    : "This source can use subtitles in its spoken language.";
}

function validateField(input, message, target) {
  input.setAttribute("aria-invalid", "true");
  const oldDescription = input.getAttribute("aria-describedby") || "";
  if (!oldDescription.split(/\s+/).includes(target.id)) {
    input.setAttribute("aria-describedby", (oldDescription + " " + target.id).trim());
  }
  const panel = input.closest("details");
  if (panel) panel.open = true;
  setStatus(target, message, "error");
  input.focus();
}

function clearInvalid(form) {
  for (const control of form.querySelectorAll('[aria-invalid="true"]')) control.removeAttribute("aria-invalid");
}

function selectedVideo(input, status) {
  const file = input.files[0];
  if (!file) {
    validateField(input, "Choose a video file first.", status);
  } else if (!/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name)) {
    validateField(input, "Choose a supported video file.", status);
  } else if (!file.size || file.size > 2 * 1024 ** 3) {
    validateField(input, "Choose a non-empty video up to 2 GiB.", status);
  } else {
    return file;
  }
  return null;
}

function clearReview(message = "Load a video and start analysis to see suggested clips here.") {
  $("naturalDurationButton").hidden = true;
  $("exportFallback").checked = false;
  state.clips.clear();
  state.settings = null;
  state.jobs.preview = {};
  saveJobs();
  $("clipList").replaceChildren();
  $("reviewEmpty").textContent = message;
  $("reviewEmpty").hidden = false;
  $("reviewNotes").hidden = true;
  $("reviewCount").textContent = "";
  $("exportPanel").hidden = true;
  setStatus($("exportSettings"), "");
  $("exportResults").hidden = true;
  $("exportBar").hidden = true;
  reconnectButton("exportReconnect", false);
  setStatus($("exportStatus"), "");
}

async function loadSource(event, usingUpload) {
  event.preventDefault();
  if (state.sourceBusy || state.analysisBusy || state.exportBusy || state.pendingClips.size) return;
  const form = event.currentTarget;
  const input = usingUpload ? $("videoFile") : $("videoUrl");
  const button = form.querySelector("button");
  clearInvalid(form);
  const value = usingUpload ? selectedVideo(input, $("sourceStatus")) : input.value.trim();
  if (!value) {
    if (!usingUpload) validateField(input, "Paste a YouTube link.", $("sourceStatus"));
    return;
  }
  if (!usingUpload) {
    try {
      const url = new URL(value);
      if (
        !["https:", "http:"].includes(url.protocol) ||
        !["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].includes(url.hostname)
      ) throw new Error();
    } catch {
      validateField(input, "Enter a valid YouTube video URL.", $("sourceStatus"));
      return;
    }
  }

  state.sourceBusy = true;
  busy(button, true);
  updateControls();
  sourceStatus(usingUpload ? "Copying video into the local workspace…" : "Retrieving YouTube metadata…");
  $("uploadProgress").hidden = !usingUpload;
  $("uploadProgress").removeAttribute("value");
  try {
    const { job_id: id } = usingUpload ? await uploadFile(value) : await api("/api/metadata", { url: value });
    $("uploadProgress").hidden = true;
    const result = await followJob(id, (job) => sourceStatus(job.message || "Reading video information…"));
    if (!result.source || result.status !== "done") throw new RequestError(result.error || result.message || "The video could not be loaded.");
    clearJob("analysis");
    clearJob("export");
    $("analysisProgress").hidden = true;
    clearReview("Start analysis to find suggested clips from this source.");
    displaySource(result.source);
    sourceStatus("Video is ready for analysis.", "success");
  } catch (error) {
    sourceStatus(error.message, "error");
  } finally {
    state.sourceBusy = false;
    $("uploadProgress").hidden = true;
    busy(button, false);
    updateControls();
  }
}

function readSettings() {
  const form = $("settingsForm");
  const errorBox = $("settingsStatus");
  clearInvalid(form);
  const formData = new FormData(form);
  const settings = Object.fromEntries(formData);
  for (const key of ["count", "min_duration", "max_duration", "target_duration", "subtitle_size"]) {
    const input = form.elements.namedItem(key);
    if (key === "subtitle_size" && input.disabled) {
      settings[key] = 54;
      continue;
    }
    if (!input.value && !input.required) {
      if (input.validity.badInput) {
        validateField(input, "Enter a valid number.", errorBox);
        return null;
      }
      settings[key] = null;
      continue;
    }
    if (!input.validity.valid) {
      const label = input.labels[0].textContent;
      const kind = input.step === "1" ? "a whole number" : "a number";
      validateField(input, label + " must be " + kind + " between " + input.min + " and " + input.max + ".", errorBox);
      return null;
    }
    settings[key] = input.valueAsNumber;
  }

  if (settings.min_duration > settings.max_duration) {
    validateField($("maxDuration"), "Maximum duration must be at least the minimum duration.", errorBox);
    return null;
  }
  if (settings.target_duration !== null && (settings.target_duration < settings.min_duration || settings.target_duration > settings.max_duration)) {
    validateField($("targetDuration"), "Exact duration must be within the minimum and maximum durations.", errorBox);
    return null;
  }
  if (settings.language === "other") {
    const input = $("languageCode");
    const code = input.value.trim().toLowerCase();
    if (!/^[a-z]{2,3}$/.test(code)) {
      validateField(input, "Enter a two- or three-letter spoken language code.", errorBox);
      return null;
    }
    settings.language = code;
  }
  settings.embed_subtitles = $("embedSubtitles").checked;
  settings.subtitle_color = $("subtitleColor").value;
  settings.subtitle_background = $("subtitleBackground").value;
  settings.subtitle_position = $("subtitlePosition").value;
  settings.source_id = state.source.id;
  return settings;
}

function displaySettings(settings) {
  const form = $("settingsForm");
  for (const [name, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  }
  if (!$("language").value) {
    $("language").value = "other";
    $("languageCode").value = settings.language || "";
  }
  $("languageCodeField").hidden = $("language").value !== "other";
}

function updateAnalysis(job) {
  $("naturalDurationButton").hidden = !(job.status === "done" && !job.clips?.length && (job.natural_duration_available || job.settings?.target_duration));
  if (job.source && (job.source.id !== state.source?.id
      || job.source.playback_url !== state.source.playback_url
      || job.source.attached_title !== state.source.attached_title)) {
    displaySource(job.source);
    sourceStatus("Video is ready for analysis.", "success");
    const mode = job.source.kind === "upload" ? "file" : "url";
    document.querySelector('input[name="source_mode"][value="' + mode + '"]').checked = true;
    switchMode(mode);
  }
  if (job.settings && !state.settings) {
    state.settings = job.settings;
    displaySettings(job.settings);
  }
  if (Array.isArray(job.clips)) {
    for (const clip of job.clips) displayClip(clip);
    const count = state.clips.size;
    $("reviewEmpty").hidden = count > 0;
    $("exportPanel").hidden = count === 0;
    updateControls();
  }
  const notes = Array.isArray(job.notes) ? job.notes.join(" ") : job.notes || "";
  if (notes) {
    $("reviewNotes").hidden = false;
    setStatus($("reviewNotes"), notes);
  }
  const message = [job.error || job.message || "", job.detected_language ? "Speech language " + job.detected_language + "." : ""].filter(Boolean).join(" ");
  setStatus($("analysisMessage"), message, job.status === "error" ? "error" : "");
  progress($("analysisBar"), $("analysisPercent"), job.progress);
}

async function trackAnalysis(id) {
  state.analysisBusy = true;
  $("analysisProgress").hidden = false;
  reconnectButton("analysisReconnect", false);
  updateControls();
  try {
    const result = await followJob(id, updateAnalysis);
    state.analysisBusy = false;
    if (!state.clips.size) {
      $("reviewEmpty").textContent = result.status === "done"
        ? "No complete speech passage fits these settings. If an exact duration is set, try Find natural-length clips. For music or action-heavy footage, review the source and try a shorter speech passage."
        : "No clips are available. Review the analysis status and try again.";
    }
  } catch (error) {
    if (error.status === 404) {
      clearJob("analysis");
      state.analysisBusy = false;
      $("reviewEmpty").textContent = "This analysis is no longer available. Load a video to start a new analysis.";
    } else {
      reconnectButton("analysisReconnect", true);
    }
    setStatus($("analysisMessage"), error.message, "error");
  } finally {
    updateControls();
  }
}

async function startAnalysis(event) {
  event.preventDefault();
  if (!state.source || $("analyzeButton").disabled) return;
  const settings = readSettings();
  if (!settings) return;
  state.analysisBusy = true;
  updateControls();
  setStatus($("settingsStatus"), "");
  try {
    const { job_id: id } = await api("/api/analyze", settings);
    clearJob("export");
    clearReview("Suggested clips will appear here as their previews are prepared.");
    state.settings = settings;
    markJob("analysis", id);
    progress($("analysisBar"), $("analysisPercent"), 0);
    setStatus($("analysisMessage"), "Waiting to start analysis…");
    await trackAnalysis(id);
  } catch (error) {
    state.analysisBusy = false;
    setStatus($("settingsStatus"), error.message, "error");
  } finally {
    updateControls();
  }
}

function getMediaURL(value, path = /^\/preview\/[a-f0-9]{32}\.mp4$/) {
  try {
    const url = new URL(value, location.href);
    if (url.origin === location.origin && path.test(url.pathname)) return url.href;
  } catch {
    // Feedback below offers to regenerate a preview when its URL is invalid.
  }
  return "";
}

function sourceRoute(action) {
  return "/api/sources/" + encodeURIComponent(state.source.id) + "/" + action;
}

function clearSourceVideo() {
  const video = $("sourceVideo");
  video.pause();
  video.hidden = true;
  if (video.hasAttribute("src")) {
    video.removeAttribute("src");
    video.load();
  }
}

function updateSourcePosition(seconds) {
  const limit = Number($("sourceRange").max);
  const value = Math.min(limit, Math.max(0, seconds));
  $("sourceRange").value = value;
  $("sourceSeconds").value = Math.round(value * 1000) / 1000;
  $("sourceRange").setAttribute("aria-valuetext", formatTime(value));
  $("sourceTime").textContent = formatTime(value) + " / " + formatTime(state.source.duration);
}

function updateSourceControls() {
  $("sourceSeekForm").hidden = Boolean(sourcePlayer.directURL);
  $("sourceSeekForm").querySelectorAll("input, button").forEach((control) => {
    control.disabled = sourcePlayer.busy;
  });
  busy($("sourcePlay"), sourcePlayer.busy);
  $("sourcePlaybackHint").textContent = sourcePlayer.directURL
    ? "Use the player controls to play or seek anywhere in the original video."
    : "Source playback loads short sections as you watch. Choose a position to play from anywhere in the video.";
  for (const { node } of state.clips.values()) {
    node.querySelector('[data-action="source-player"]').disabled = !state.source || sourcePlayer.busy;
  }
}

function configureSourcePlayer(source) {
  const directURL = getMediaURL(source.playback_url, /^\/source\/[a-f0-9]{32}\/original$/);
  $("sourceContext").hidden = false;
  if (sourcePlayer.id === source.id && sourcePlayer.configuredURL === directURL) return;
  const newSource = sourcePlayer.id !== source.id;
  Object.assign(sourcePlayer, {
    id: source.id, directURL, configuredURL: directURL, generation: sourcePlayer.generation + 1,
    busy: false, offset: 0, windowEnd: 0, pendingSeek: null, autoplay: false, draft: false,
  });
  clearSourceVideo();
  if (newSource) $("sourceContext").open = false;
  const lastMoment = Math.max(0, Math.floor(source.duration * 1000 - 1) / 1000);
  $("sourceRange").max = lastMoment;
  $("sourceSeconds").max = lastMoment;
  updateSourcePosition(0);
  updateSourceControls();
  setStatus($("sourcePlaybackStatus"), "");
  if ($("sourceContext").open && directURL) loadOriginal();
}

function loadOriginal(seconds = null) {
  const video = $("sourceVideo");
  if (seconds !== null) sourcePlayer.pendingSeek = seconds;
  video.hidden = false;
  if (video.src !== sourcePlayer.directURL) {
    video.preload = "metadata";
    video.src = sourcePlayer.directURL;
  } else if (video.readyState >= 1) {
    positionSourceVideo();
  }
}

function positionSourceVideo() {
  const video = $("sourceVideo");
  const { pendingSeek, offset, autoplay, generation } = sourcePlayer;
  sourcePlayer.pendingSeek = null;
  sourcePlayer.autoplay = false;
  if (pendingSeek !== null) {
    video.currentTime = Math.max(0, pendingSeek - offset);
  }
  if (autoplay) video.play().catch(() => {
    if (generation === sourcePlayer.generation && $("sourceContext").open && !video.error) {
      setStatus($("sourcePlaybackStatus"), "Press Play in the source player to continue.");
    }
  });
}

async function loadSourceWindow(seconds, autoplay) {
  if (!state.source || sourcePlayer.busy) return;
  const generation = ++sourcePlayer.generation;
  const current = () => generation === sourcePlayer.generation;
  const video = $("sourceVideo");
  sourcePlayer.busy = true;
  video.pause();
  video.hidden = true;
  updateSourcePosition(seconds);
  updateSourceControls();
  setStatus($("sourcePlaybackStatus"), "Loading source at " + formatTime(seconds) + "…");
  try {
    const { job_id: id } = await api(sourceRoute("playback"), { time: seconds });
    const job = await followJob(id, () => {}, current);
    if (!current()) return;
    const url = getMediaURL(job.playback_url, /^\/context\/[a-f0-9]{32}\.mp4$/);
    if (job.status !== "done" || !url || !Number.isFinite(job.start) || !(job.end > job.start)) {
      throw new RequestError(job.error || "The source section could not be played.");
    }
    Object.assign(sourcePlayer, {
      offset: job.start, windowEnd: job.end, pendingSeek: seconds,
      autoplay: autoplay && $("sourceContext").open, draft: false,
    });
    video.hidden = false;
    if (video.src === url && video.readyState >= 1) {
      positionSourceVideo();
    } else {
      video.preload = "metadata";
      video.src = url;
    }
    updateSourcePosition(seconds);
    setStatus($("sourcePlaybackStatus"), "");
  } catch (error) {
    if (!current()) return;
    video.hidden = !video.hasAttribute("src") || Boolean(video.error);
    setStatus($("sourcePlaybackStatus"), error.message + (state.source.kind === "youtube" && !state.source.attached_title
      ? " Retry this position or attach the same full-length video."
      : " Try playing from this position again."), "error");
  } finally {
    if (current()) {
      sourcePlayer.busy = false;
      updateSourceControls();
    }
  }
}

function viewSource(seconds) {
  $("sourceContext").open = true;
  $("sourceContextToggle").focus();
  if (sourcePlayer.directURL) {
    loadOriginal(seconds);
  } else {
    updateSourcePosition(seconds);
    void loadSourceWindow(seconds, false);
  }
}

async function attachVideo(event) {
  event.preventDefault();
  if ($("recoveryFile").disabled || state.source?.kind !== "youtube" || state.source.attached_title) return;
  const status = $("recoveryStatus");
  const form = event.currentTarget;
  clearInvalid(form);
  const file = selectedVideo($("recoveryFile"), status);
  if (!file) return;
  const fields = state.jobs.analysis ? { analysis_job_id: state.jobs.analysis } : {};
  const bar = $("recoveryProgress");
  const button = form.querySelector("button");
  state.sourceBusy = true;
  updateControls();
  busy(button, true);
  bar.hidden = false;
  bar.removeAttribute("value");
  setStatus(status, "Copying the matching video into the workspace…");
  try {
    const { job_id: id } = await uploadFile(file, sourceRoute("attach"), "recoveryProgress", fields);
    const result = await followJob(id, (job) => {
      progress(bar, null, job.progress);
      setStatus(status, job.message || "Preparing the matching video…");
      if (job.clips) for (const clip of job.clips) displayClip(clip);
    });
    if (result.status !== "done" || !result.source) {
      throw new RequestError(result.error || "The matching video could not be attached.");
    }
    displaySource(result.source);
    if (result.clips) for (const clip of result.clips) displayClip(clip);
    form.reset();
    setStatus(status, result.message || "Matching video attached.", "success");
    $("attachRecovery").querySelector("summary").focus();
  } catch (error) {
    setStatus(status, error.message, "error");
  } finally {
    state.sourceBusy = false;
    bar.hidden = true;
    busy(button, false);
    updateControls();
  }
}

function exportable(clip) {
  return clip.review === "approved" && clip.preview_status === "ready";
}

function updateClipActions(record) {
  const { node, clip, playbackError } = record;
  const busy = state.exportBusy || state.sourceBusy || state.pendingClips.has(clip.id);
  const locked = busy || clip.preview_status === "pending";
  const approve = node.querySelector('[data-action="approve"]');
  const reject = node.querySelector('[data-action="reject"]');
  const retry = node.querySelector('[data-action="retry"]');
  approve.textContent = clip.review === "approved" ? "Undo approval" : "Approve";
  reject.textContent = clip.review === "rejected" ? "Undo rejection" : "Reject";
  approve.disabled = locked || (clip.review !== "approved" && (clip.preview_status !== "ready" || playbackError));
  reject.disabled = locked;
  node.querySelector('[data-action="edit"]').disabled = locked;
  const reconnect = Boolean(state.jobs.preview[clip.id]);
  retry.hidden = !(playbackError || clip.preview_status === "error" || reconnect);
  retry.disabled = busy;
  retry.textContent = reconnect ? "Reconnect to preview" : "Retry preview";
  const attach = node.querySelector('[data-action="attach"]');
  attach.hidden = !(clip.preview_status === "error" && state.source?.kind === "youtube" && !state.source.attached_title);
  attach.disabled = busy || state.analysisBusy;
  const source = node.querySelector('[data-action="source-player"]');
  source.disabled = !state.source || sourcePlayer.busy;
  for (const download of node.querySelectorAll('[data-action="download"], [data-action="save-preview"]')) {
    download.hidden = !exportable(clip);
    download.disabled = busy || state.analysisBusy || state.pendingClips.size > 0;
  }
  for (const control of node.querySelectorAll(".clip-edit input, .clip-edit button")) control.disabled = locked;
  node.setAttribute("aria-busy", String(state.pendingClips.has(clip.id)));
}

function updatePlayer(record) {
  const { node, clip, mediaURL, playbackError } = record;
  const video = node.querySelector("video");
  const ready = Boolean(mediaURL) && !playbackError;
  video.hidden = !ready;
  node.querySelector(".media-placeholder").hidden = ready;
  const pending = clip.preview_status === "pending";
  setStatus(node.querySelector(".preview-state"), pending ? "Preparing MP4" : "MP4 unavailable");
  setStatus(node.querySelector(".preview-message"), playbackError ? "The browser could not play the MP4. Try regenerating it."
    : pending ? "You can check the timestamps and context while the cut renders."
      : (clip.preview_error || "The preview could not be prepared.") + " Retry the preview to make the selected cut.");
}

function displayClip(clip) {
  let record = state.clips.get(clip.id);
  if (!record) {
    const node = $("clipTemplate").content.firstElementChild.cloneNode(true);
    const status = node.querySelector(".clip-status");
    const edit = node.querySelector(".clip-edit");
    status.id = "clip-status-" + clip.id;
    edit.id = "clip-edit-" + clip.id;
    node.querySelector('[data-action="edit"]').setAttribute("aria-controls", edit.id);
    record = { node, clip, mediaURL: null, playbackError: false };
    state.clips.set(clip.id, record);
    node.addEventListener("click", (event) => handleClipAction(record, event));
    edit.addEventListener("submit", (event) => submitEdit(record, event));
    node.querySelector("video").addEventListener("error", () => {
      if (!record.mediaURL) return;
      record.playbackError = true;
      updatePlayer(record);
      updateClipActions(record);
    });
    node.querySelector("video").addEventListener("play", () => {
      $("sourceVideo").pause();
      for (const other of state.clips.values()) {
        if (other === record) continue;
        other.node.querySelector("video").pause();
      }
    });
    $("clipList").append(node);
  }

  record.clip = clip;
  const { node } = record;
  const text = (selector, value) => {
    const element = node.querySelector(selector);
    if (element.textContent !== value) element.textContent = value;
  };
  text(".clip-category", categories[clip.category] || clip.category || "Highlight");
  text(".clip-review-state", reviews[clip.review] || reviews.pending);
  const badge = node.querySelector(".clip-review-state");
  badge.classList.toggle("is-approved", clip.review === "approved");
  badge.classList.toggle("is-rejected", clip.review === "rejected");
  text(".clip-hook", clip.hook || "Suggested highlight");
  text(".source-range", formatTime(clip.start, true) + " – " + formatTime(clip.end, true));
  text(".clip-duration", secondsLabel(clip.duration));
  text(".clip-reason", clip.edited && clip.reason ? "Original selection. " + clip.reason : clip.reason || "");
  const showScore = Number.isFinite(clip.score) && !clip.edited;
  text(".clip-score", clip.edited ? "Manually adjusted cut" : showScore ? "Suitability " + clip.score + "/100" : "");
  node.querySelector(".clip-transcript").hidden = !clip.text;
  text(".clip-transcript p", clip.text || "");

  const totalDuration = state.source?.duration;
  const track = node.querySelector(".clip-track");
  track.hidden = !totalDuration;
  if (totalDuration) {
    track.style.setProperty("--cut-start", String(clip.start / totalDuration * 100) + "%");
    track.style.setProperty("--cut-width", String((clip.end - clip.start) / totalDuration * 100) + "%");
  }

  const video = node.querySelector("video");
  const newURL = clip.preview_status === "ready" && clip.preview_url ? getMediaURL(clip.preview_url) : "";
  if (clip.preview_status === "ready" && !newURL) record.playbackError = true;
  if (newURL !== record.mediaURL) {
    record.mediaURL = newURL;
    record.playbackError = clip.preview_status === "ready" && !newURL;
    if (newURL) {
      video.src = newURL;
    } else if (video.hasAttribute("src")) {
      video.removeAttribute("src");
      video.load();
    }
  }
  video.setAttribute("aria-label", "Preview from " + formatTime(clip.start) + " to " + formatTime(clip.end));
  updatePlayer(record);
  updateClipActions(record);
  return record;
}

function toggleEdit(record, visible) {
  const { clip, node } = record;
  const edit = node.querySelector(".clip-edit");
  const trigger = node.querySelector('[data-action="edit"]');
  edit.hidden = !visible;
  trigger.setAttribute("aria-expanded", String(visible));
  if (visible) {
    edit.elements.start.value = clip.start;
    edit.elements.end.value = clip.end;
    clearInvalid(edit);
    setStatus(node.querySelector(".clip-status"), "");
    edit.elements.start.focus();
  }
  return trigger;
}

async function handleClipAction(record, event) {
  const action = event.target.closest("button[data-action]")?.dataset.action;
  if (!action) return;
  const { clip, node } = record;
  if (action === "source-player") {
    viewSource(clip.start);
    return;
  }
  if (action === "attach") {
    $("attachRecovery").open = true;
    $("recoveryFile").focus();
    return;
  }
  if (action === "download" || action === "save-preview") {
    await startExport([clip.id], action === "save-preview");
    return;
  }
  if (action === "edit" || action === "cancel-edit") {
    const show = action === "edit" && node.querySelector(".clip-edit").hidden;
    const button = toggleEdit(record, show);
    if (!show) button.focus();
    return;
  }
  if (action === "retry") {
    const id = state.jobs.preview[clip.id];
    if (id) await trackPreview(record, id, false);
    else await previewClip(record, {});
    return;
  }

  const requested = action === "approve" ? "approved" : "rejected";
  const review = clip.review === requested ? "pending" : requested;
  state.pendingClips.add(clip.id);
  updateControls();
  const status = node.querySelector(".clip-status");
  setStatus(status, "Saving review…");
  try {
    const result = await api("/api/clips/" + encodeURIComponent(clip.id) + "/review", { review });
    displayClip(result.clip);
    setStatus(status, reviews[review] + ".", review === "approved" ? "success" : "");
  } catch (error) {
    setStatus(status, error.message, "error");
  } finally {
    state.pendingClips.delete(clip.id);
    updateControls();
    node.querySelector('[data-action="' + action + '"]').focus();
  }
}

async function submitEdit(record, event) {
  event.preventDefault();
  if (state.pendingClips.has(record.clip.id) || state.exportBusy || state.analysisBusy) return;
  const edit = event.currentTarget;
  const error = record.node.querySelector(".clip-status");
  const startField = edit.elements.start;
  const endField = edit.elements.end;
  const start = startField.valueAsNumber;
  const end = endField.valueAsNumber;
  clearInvalid(edit);
  if (!Number.isFinite(start) || start < 0 || start >= state.source.duration) {
    validateField(startField, "Enter a start time within the source video.", error);
    return;
  }
  if (!Number.isFinite(end) || end <= start || end > state.source.duration) {
    validateField(endField, "Enter an end time after the start and within the source video.", error);
    return;
  }
  const duration = Math.round((end - start) * 1000) / 1000;
  if (duration < 5 || duration > 180) {
    validateField(endField, "Edited clips must be between 5 and 180 seconds.", error);
    return;
  }
  await previewClip(record, { start, end });
}

async function previewClip(record, selection) {
  const { clip, node } = record;
  const status = node.querySelector(".clip-status");
  const editing = "start" in selection;
  if (state.pendingClips.has(clip.id) || state.exportBusy || clip.preview_status === "pending") return;
  state.pendingClips.add(clip.id);
  updateControls();
  setStatus(status, editing ? "Rendering the adjusted section…" : "Preparing a new preview…");
  try {
    const { job_id: id } = await api("/api/clips/" + encodeURIComponent(clip.id) + "/preview", selection);
    state.jobs.preview[clip.id] = id;
    saveJobs();
    displayClip({ ...clip, review: "pending", preview_status: "pending" });
    await trackPreview(record, id, editing);
  } catch (error) {
    setStatus(status, error.message, "error");
    state.pendingClips.delete(clip.id);
    updateControls();
    if (editing) node.querySelector(".clip-edit button[type='submit']").focus();
    else node.querySelector('[data-action="retry"]').focus();
  }
}

async function trackPreview(record, id, editing) {
  const { clip, node } = record;
  const status = node.querySelector(".clip-status");
  state.pendingClips.add(clip.id);
  updateControls();
  let rendered = false;
  try {
    const result = await followJob(id, (job) => {
      if (job.clip) displayClip(job.clip);
      setStatus(status, job.message || "Rendering preview…");
    });
    delete state.jobs.preview[clip.id];
    if (result.status !== "done" || result.clip?.preview_status !== "ready") {
      throw new RequestError(result.error || result.clip?.preview_error || "The selected section could not be rendered.");
    }
    rendered = true;
    if (editing) toggleEdit(record, false);
    setStatus(status, editing ? "Updated preview is ready. Review this cut before approving." : "New preview is ready.", "success");
  } catch (error) {
    if (error.status === 404) {
      delete state.jobs.preview[clip.id];
      if (record.clip.preview_status === "pending") {
        displayClip({ ...record.clip, preview_status: "error", preview_error: "The preview job is no longer available." });
      }
    }
    setStatus(status, error.message, "error");
  } finally {
    saveJobs();
    state.pendingClips.delete(clip.id);
    updateControls();
    if (editing && !rendered) {
      const submit = node.querySelector(".clip-edit button[type='submit']");
      if (!submit.disabled) submit.focus();
    } else if (editing) {
      node.querySelector('[data-action="edit"]').focus();
    }
  }
}

function displayExport(job) {
  $("exportBar").hidden = terminalStatuses.has(job.status);
  progress($("exportBar"), null, job.progress);
  const tone = job.status === "error" ? "error" : job.status === "done" ? "success" : "";
  setStatus($("exportStatus"), job.error || job.message || "", tone);
  const files = job.files || [];
  const failed = job.failed || [];
  if (!files.length && !failed.length) return;
  $("exportResults").hidden = false;
  $("exportDestination").textContent = job.destination ? "Saved to " + job.destination : "";
  const rows = files.map((file) => {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = file.name;
    li.append(title);
    const info = document.createElement("p");
    info.className = "help";
    const specs = [];
    if (file.width && file.height) specs.push(file.width + " × " + file.height);
    if (Number.isFinite(file.duration)) specs.push(secondsLabel(file.duration));
    if (file.transcript) specs.push("Transcript " + file.transcript);
    info.textContent = specs.join(" · ");
    li.append(info);
    if (file.quality_note) {
      const note = document.createElement("p");
      note.className = "help";
      note.textContent = file.quality_note;
      li.append(note);
    }
    return li;
  });
  $("exportFiles").replaceChildren(...rows);
  $("exportFailed").replaceChildren(...failed.map((entry) => {
    const li = document.createElement("li");
    li.textContent = (state.clips.get(entry.id)?.clip.hook || "Clip") + " — " + entry.error;
    return li;
  }));
}

async function trackExport(id) {
  state.exportBusy = true;
  $("exportPanel").hidden = false;
  reconnectButton("exportReconnect", false);
  updateControls();
  try {
    await followJob(id, displayExport);
    state.exportBusy = false;
  } catch (error) {
    if (error.status === 404) {
      clearJob("export");
      state.exportBusy = false;
    } else {
      reconnectButton("exportReconnect", true);
    }
    setStatus($("exportStatus"), error.message, "error");
  } finally {
    updateControls();
  }
}

async function startExport(clipIds = null, savePreview = false) {
  if ($("exportButton").disabled) return;
  const ids = [...state.clips.values()]
    .filter(({ clip }) => exportable(clip) && (!clipIds || clipIds.includes(clip.id)))
    .map(({ clip }) => clip.id);
  if (!ids.length) return;
  state.exportBusy = true;
  updateControls();
  $("exportResults").hidden = true;
  reconnectButton("exportReconnect", false);
  $("exportBar").hidden = true;
  setStatus($("exportStatus"), "Opening the folder picker on this computer…");
  $("exportTitle").focus();
  try {
    const options = { clip_ids: ids };
    if (savePreview) options.mode = "preview";
    else options.allow_preview_fallback = !$("exportFallbackOption").hidden && $("exportFallback").checked;
    const { job_id: id } = await api("/api/export", options);
    markJob("export", id);
    await trackExport(id);
  } catch (error) {
    state.exportBusy = false;
    setStatus($("exportStatus"), error.message, "error");
  } finally {
    updateControls();
  }
}

async function restoreWorkspace() {
  try {
    const saved = JSON.parse(localStorage.getItem(sessionKey) || "{}");
    for (const type of ["analysis", "export"]) {
      if (typeof saved?.[type] === "string") state.jobs[type] = saved[type];
    }
    for (const [clip, job] of Object.entries(saved?.preview || {})) {
      if (/^[0-9a-f]{32}$/.test(clip) && typeof job === "string") state.jobs.preview[clip] = job;
    }
  } catch {
    // A corrupted or unavailable session record does not block a new workspace.
  }
  if (state.jobs.analysis) await trackAnalysis(state.jobs.analysis);
  for (const [id, job] of Object.entries(state.jobs.preview)) {
    const record = state.clips.get(id);
    if (record) void trackPreview(record, job, false);
  }
  if (state.jobs.export) await trackExport(state.jobs.export);
}

for (const input of document.querySelectorAll('input[name="source_mode"]')) {
  input.addEventListener("change", () => switchMode(input.value));
}
$("videoThumb").addEventListener("error", () => { $("thumbnailFrame").hidden = true; });
$("urlForm").addEventListener("submit", (event) => loadSource(event, false));
$("uploadForm").addEventListener("submit", (event) => loadSource(event, true));
$("recoveryForm").addEventListener("submit", attachVideo);
$("sourceContext").addEventListener("toggle", () => {
  if (!$("sourceContext").open) $("sourceVideo").pause();
  else if (sourcePlayer.directURL) loadOriginal();
});
$("sourceRange").addEventListener("input", (event) => {
  sourcePlayer.draft = true;
  updateSourcePosition(event.target.valueAsNumber);
});
$("sourceSeconds").addEventListener("input", (event) => {
  sourcePlayer.draft = true;
  const seconds = event.target.valueAsNumber;
  if (Number.isFinite(seconds)) {
    $("sourceRange").value = seconds;
    $("sourceRange").setAttribute("aria-valuetext", formatTime(Number($("sourceRange").value)));
    $("sourceTime").textContent = formatTime(Number($("sourceRange").value)) + " / " + formatTime(state.source.duration);
  }
});
$("sourceSeekForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (sourcePlayer.busy) return;
  const field = $("sourceSeconds");
  clearInvalid(event.currentTarget);
  if (!field.validity.valid) {
    validateField(field, "Enter seconds within the source video.", $("sourcePlaybackStatus"));
    return;
  }
  void loadSourceWindow(field.valueAsNumber, true);
});
$("sourceVideo").addEventListener("loadedmetadata", positionSourceVideo);
$("sourceVideo").addEventListener("timeupdate", (event) => {
  if (state.source && !sourcePlayer.directURL && !sourcePlayer.busy && !sourcePlayer.draft && !event.target.hidden) {
    updateSourcePosition(sourcePlayer.offset + event.target.currentTime);
  }
});
$("sourceVideo").addEventListener("play", () => {
  for (const { node } of state.clips.values()) node.querySelector("video").pause();
});
$("sourceVideo").addEventListener("ended", () => {
  const { directURL, offset, windowEnd } = sourcePlayer;
  if (!directURL && $("sourceContext").open && windowEnd > offset && windowEnd < state.source.duration - 0.001) {
    void loadSourceWindow(windowEnd, true);
  }
});
$("sourceVideo").addEventListener("error", (event) => {
  if (!event.target.hasAttribute("src")) return;
  const status = $("sourcePlaybackStatus");
  if (sourcePlayer.directURL) {
    sourcePlayer.directURL = "";
    clearSourceVideo();
    updateSourceControls();
    setStatus(status, "This browser could not play the original file directly. Choose a source position to play a compatible section.", "error");
  } else {
    event.target.hidden = true;
    setStatus(status, "This source section could not play. Try playing from this position again.", "error");
  }
});
$("naturalDurationButton").addEventListener("click", () => {
  if ($("analyzeButton").disabled) return;
  $("targetDuration").value = "";
  $("settingsForm").requestSubmit();
});
$("settingsForm").addEventListener("submit", startAnalysis);
$("embedSubtitles").addEventListener("change", updateControls);
$("language").addEventListener("change", () => { $("languageCodeField").hidden = $("language").value !== "other"; });
$("exportButton").addEventListener("click", () => startExport());
$("analysisReconnect").addEventListener("click", () => trackAnalysis(state.jobs.analysis));
$("exportReconnect").addEventListener("click", () => trackExport(state.jobs.export));
updateControls();
restoreWorkspace();
