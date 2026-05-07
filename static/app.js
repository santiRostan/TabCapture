const state = {
  sourceId: null,
  duration: null,
  cropStart: 0,
  cropEnd: 0.46,
  activeHandle: null,
  pollTimer: null,
  importing: false,
  previewLoading: false,
  previewRequestId: 0,
  previewTime: null,
  pendingPreviewTime: null,
  pendingPreviewSourceId: null,
};

const els = {
  importPanel: document.getElementById("importPanel"),
  importForm: document.getElementById("importForm"),
  importButton: document.getElementById("importButton"),
  importStatus: document.getElementById("importStatus"),
  workspace: document.getElementById("workspace"),
  youtubeUrl: document.getElementById("youtubeUrl"),
  localFile: document.getElementById("localFile"),
  previewPanel: document.getElementById("previewPanel"),
  refreshPreview: document.getElementById("refreshPreview"),
  previewFrame: document.getElementById("previewFrame"),
  previewImage: document.getElementById("previewImage"),
  previewLoadingOverlay: document.getElementById("previewLoadingOverlay"),
  previewStatus: document.getElementById("previewStatus"),
  shadeTop: document.getElementById("shadeTop"),
  shadeBottom: document.getElementById("shadeBottom"),
  cropWindow: document.getElementById("cropWindow"),
  cropTop: document.getElementById("cropTop"),
  cropBottom: document.getElementById("cropBottom"),
  cropReadout: document.getElementById("cropReadout"),
  titleInput: document.getElementById("titleInput"),
  channelInput: document.getElementById("channelInput"),
  startInput: document.getElementById("startInput"),
  endInput: document.getElementById("endInput"),
  outputInput: document.getElementById("outputInput"),
  sampleEvery: document.getElementById("sampleEvery"),
  diffThreshold: document.getElementById("diffThreshold"),
  bandHalfWidth: document.getElementById("bandHalfWidth"),
  compareWindow: document.getElementById("compareWindow"),
  generateButton: document.getElementById("generateButton"),
  extractStatus: document.getElementById("extractStatus"),
  resultPanel: document.getElementById("resultPanel"),
  resultTitle: document.getElementById("resultTitle"),
  resultMeta: document.getElementById("resultMeta"),
  resultPdfLink: document.getElementById("resultPdfLink"),
  jobPanel: document.getElementById("jobPanel"),
  jobPhase: document.getElementById("jobPhase"),
  statChecked: document.getElementById("statChecked"),
  statSkipped: document.getElementById("statSkipped"),
  statKept: document.getElementById("statKept"),
  logBox: document.getElementById("logBox"),
};

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

function setStatus(element, message, mode = "") {
  element.textContent = message;
  element.dataset.mode = mode;
}

function setControlBusy(controls, busy) {
  controls.forEach((control) => {
    control.disabled = busy;
  });
}

function setImportLoading(isLoading) {
  state.importing = isLoading;
  els.importPanel.setAttribute("aria-busy", String(isLoading));
  els.importPanel.classList.toggle("is-loading", isLoading);
  els.importButton.textContent = isLoading ? "Importing..." : "Import video";
  setControlBusy([els.importButton, els.youtubeUrl, els.localFile], isLoading);
}

function setPreviewLoading(isLoading, message = "Updating preview...", mode = "") {
  state.previewLoading = isLoading;
  els.previewPanel.setAttribute("aria-busy", String(isLoading));
  els.previewFrame.setAttribute("aria-busy", String(isLoading));
  els.previewFrame.classList.toggle("is-loading", isLoading);
  els.refreshPreview.disabled = isLoading;
  els.previewStatus.textContent = message;
  els.previewStatus.dataset.mode = mode;
  els.previewLoadingOverlay.classList.toggle("hidden", !isLoading && !message);
  els.previewLoadingOverlay.classList.toggle("is-error", mode === "error");
  els.previewLoadingOverlay.classList.remove("is-stale");
}

function clearPreviewStatus() {
  setPreviewLoading(false, "");
}

function setPreviewNotice(message, mode = "") {
  state.previewLoading = false;
  els.previewPanel.setAttribute("aria-busy", "false");
  els.previewFrame.setAttribute("aria-busy", "false");
  els.previewFrame.classList.remove("is-loading");
  els.refreshPreview.disabled = false;
  els.previewStatus.textContent = message;
  els.previewStatus.dataset.mode = mode;
  els.previewLoadingOverlay.classList.toggle("hidden", !message);
  els.previewLoadingOverlay.classList.toggle("is-error", mode === "error");
  els.previewLoadingOverlay.classList.toggle("is-stale", mode === "stale");
}

function formatSeconds(value) {
  if (!Number.isFinite(value)) return "0s";
  const rounded = Math.max(0, Math.round(value));
  const minutes = Math.floor(rounded / 60);
  const seconds = rounded % 60;
  if (minutes === 0) return `${seconds}s`;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function currentStartTime() {
  const value = numericValue(els.startInput, 0);
  return Number.isFinite(value) ? value : 0;
}

function markPreviewStale() {
  if (state.previewTime === null || state.previewLoading) return;
  if (currentStartTime() === state.previewTime) {
    clearPreviewStatus();
    return;
  }
  setPreviewNotice(`Preview is still showing ${formatSeconds(state.previewTime)}`, "stale");
}

function formatPercent(value) {
  return `${Math.round(value * 100)}%`;
}

function updateCropUi() {
  const top = state.cropStart * 100;
  const bottom = (1 - state.cropEnd) * 100;
  const height = (state.cropEnd - state.cropStart) * 100;

  els.shadeTop.style.height = `${top}%`;
  els.shadeBottom.style.height = `${bottom}%`;
  els.cropWindow.style.top = `${top}%`;
  els.cropWindow.style.height = `${height}%`;
  els.cropTop.style.top = `${top}%`;
  els.cropBottom.style.top = `${state.cropEnd * 100}%`;
  els.cropReadout.textContent = `Crop ${formatPercent(state.cropStart)} - ${formatPercent(state.cropEnd)}`;
}

function pointerRatio(event) {
  const rect = els.previewFrame.getBoundingClientRect();
  return clamp((event.clientY - rect.top) / rect.height, 0, 1);
}

function beginDrag(handle, event) {
  event.preventDefault();
  state.activeHandle = handle;
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function dragCrop(event) {
  if (!state.activeHandle) return;
  const ratio = pointerRatio(event);
  const gap = 0.05;

  if (state.activeHandle === "top") {
    state.cropStart = clamp(ratio, 0, state.cropEnd - gap);
  } else {
    state.cropEnd = clamp(ratio, state.cropStart + gap, 1);
  }
  updateCropUi();
}

function endDrag() {
  state.activeHandle = null;
}

function resetExtractionState() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  els.generateButton.disabled = false;
  els.resultPanel.classList.add("hidden");
  els.jobPanel.classList.add("hidden");
  els.jobPhase.textContent = "Queued";
  els.statChecked.textContent = "0";
  els.statSkipped.textContent = "0";
  els.statKept.textContent = "0";
  els.logBox.textContent = "";
  setStatus(els.extractStatus, "");
}

function applySource(source) {
  state.previewRequestId += 1;
  state.previewTime = null;
  state.pendingPreviewTime = null;
  state.pendingPreviewSourceId = null;
  clearPreviewStatus();
  resetExtractionState();

  state.sourceId = source.id;
  state.duration = source.duration || null;
  const metadata = source.metadata || {};
  els.titleInput.value = metadata.display_title || metadata.raw_title || "";
  els.channelInput.value = metadata.channel || "";
  els.workspace.classList.remove("hidden");

  if (state.duration) {
    const max = Math.floor(state.duration);
    const midpoint = Math.floor(max / 2);
    els.startInput.max = String(max);
    els.endInput.max = String(max);
    els.startInput.value = String(midpoint);
  } else {
    els.startInput.removeAttribute("max");
    els.endInput.removeAttribute("max");
    els.startInput.value = "0";
  }
  els.endInput.value = "";

  updateCropUi();
}

async function loadPreview(timeValue) {
  if (!state.sourceId) return;
  const time = Number.isFinite(Number(timeValue)) ? Number(timeValue) : 0;
  const sourceId = state.sourceId;

  if (state.previewLoading) {
    state.pendingPreviewTime = time;
    state.pendingPreviewSourceId = sourceId;
    state.previewRequestId += 1;
    els.previewStatus.textContent = "Updating preview...";
    return;
  }

  const requestId = state.previewRequestId + 1;
  state.previewRequestId = requestId;
  setPreviewLoading(true, "Updating preview...");

  try {
    const data = await fetchJson("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, time }),
    });
    if (requestId !== state.previewRequestId || sourceId !== state.sourceId) return;
    els.previewImage.src = `${data.preview_url}?v=${Date.now()}`;
    state.previewTime = time;
    if (currentStartTime() === state.previewTime) {
      clearPreviewStatus();
    } else {
      setPreviewNotice(`Preview is still showing ${formatSeconds(state.previewTime)}`, "stale");
    }
  } catch (error) {
    if (requestId !== state.previewRequestId || sourceId !== state.sourceId) return;
    setPreviewLoading(false, error.message, "error");
  } finally {
    if (sourceId !== state.sourceId) return;
    if (state.pendingPreviewTime !== null && state.pendingPreviewSourceId === state.sourceId) {
      const nextTime = state.pendingPreviewTime;
      state.pendingPreviewTime = null;
      state.pendingPreviewSourceId = null;
      state.previewLoading = false;
      loadPreview(nextTime);
    } else if (requestId === state.previewRequestId) {
      state.previewLoading = false;
    }
  }
}

function loadStartPreview() {
  return loadPreview(numericValue(els.startInput, 0));
}

function numericValue(input, fallback = null) {
  if (input.value === "") return fallback;
  return Number(input.value);
}

function buildExtractPayload() {
  return {
    source_id: state.sourceId,
    title: els.titleInput.value.trim(),
    channel: els.channelInput.value.trim(),
    start: numericValue(els.startInput, 0),
    end: numericValue(els.endInput, null),
    output: els.outputInput.value.trim() || "tablatura.pdf",
    crop_y_start: state.cropStart,
    crop_y_end: state.cropEnd,
    sample_every: numericValue(els.sampleEvery, 2),
    diff_threshold: numericValue(els.diffThreshold, 0.01),
    band_half_width: numericValue(els.bandHalfWidth, 90),
    compare_window: numericValue(els.compareWindow, 1),
  };
}

function renderJob(job) {
  els.jobPanel.classList.remove("hidden");
  els.jobPhase.textContent = job.status === "error" ? "Error" : job.phase || job.status;

  if (job.stats) {
    els.statChecked.textContent = job.stats.frames_checked ?? 0;
    els.statSkipped.textContent = job.stats.duplicates_skipped ?? 0;
    els.statKept.textContent = job.stats.captures_kept ?? 0;
  }

  els.logBox.textContent = (job.logs || []).join("\n");
  els.logBox.scrollTop = els.logBox.scrollHeight;

  if (job.status === "done") {
    const kept = job.stats?.captures_kept ?? 0;
    const fileName = job.pdf_name || "tablatura.pdf";
    els.resultTitle.textContent = "PDF ready";
    els.resultMeta.textContent = `${fileName} is ready with ${kept} captured tab image${kept === 1 ? "" : "s"}.`;
    els.resultPdfLink.href = job.pdf_url;
    els.resultPanel.classList.remove("hidden");
    setStatus(els.extractStatus, "PDF ready.", "success");
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    els.generateButton.disabled = false;
    els.resultPanel.scrollIntoView({ behavior: "smooth", block: "center" });
    els.resultPanel.focus({ preventScroll: true });
  } else if (job.status === "error") {
    els.resultPanel.classList.add("hidden");
    setStatus(els.extractStatus, job.error || "Extraction failed.", "error");
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    els.generateButton.disabled = false;
  } else {
    setStatus(els.extractStatus, "Generating...");
  }
}

async function pollJob(jobId) {
  try {
    const job = await fetchJson(`/api/jobs/${jobId}`);
    renderJob(job);
    return job;
  } catch (error) {
    setStatus(els.extractStatus, error.message, "error");
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    els.generateButton.disabled = false;
    return null;
  }
}

els.importForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.importing) return;

  const formData = new FormData(els.importForm);
  setImportLoading(true);
  setStatus(els.importStatus, "Reading video info...");
  try {
    const source = await fetchJson("/api/import", {
      method: "POST",
      body: formData,
    });
    applySource(source);
    setStatus(els.importStatus, "Video imported.", "success");
    await loadStartPreview();
  } catch (error) {
    setStatus(els.importStatus, error.message, "error");
  } finally {
    setImportLoading(false);
  }
});

els.refreshPreview.addEventListener("click", () => {
  loadStartPreview();
});

els.startInput.addEventListener("change", () => {
  markPreviewStale();
});

els.youtubeUrl.addEventListener("input", () => {
  if (els.youtubeUrl.value.trim()) {
    els.localFile.value = "";
  }
});

els.localFile.addEventListener("change", () => {
  if (els.localFile.files.length > 0) {
    els.youtubeUrl.value = "";
  }
});

els.cropTop.addEventListener("pointerdown", (event) => beginDrag("top", event));
els.cropBottom.addEventListener("pointerdown", (event) => beginDrag("bottom", event));
window.addEventListener("pointermove", dragCrop);
window.addEventListener("pointerup", endDrag);
window.addEventListener("pointercancel", endDrag);

els.generateButton.addEventListener("click", async () => {
  if (!state.sourceId) {
    setStatus(els.extractStatus, "Import a video first.", "error");
    return;
  }

  els.generateButton.disabled = true;
  els.resultPanel.classList.add("hidden");
  setStatus(els.extractStatus, "Starting...");
  try {
    const data = await fetchJson("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildExtractPayload()),
    });
    const firstJob = await pollJob(data.job_id);
    if (firstJob && !["done", "error"].includes(firstJob.status)) {
      state.pollTimer = setInterval(() => pollJob(data.job_id), 1000);
    }
  } catch (error) {
    setStatus(els.extractStatus, error.message, "error");
    els.generateButton.disabled = false;
  }
});

updateCropUi();
