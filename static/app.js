const state = {
  sourceId: null,
  duration: null,
  cropStart: 0,
  cropEnd: 0.46,
  activeHandle: null,
  pollTimer: null,
};

const els = {
  importForm: document.getElementById("importForm"),
  importStatus: document.getElementById("importStatus"),
  workspace: document.getElementById("workspace"),
  youtubeUrl: document.getElementById("youtubeUrl"),
  localFile: document.getElementById("localFile"),
  refreshPreview: document.getElementById("refreshPreview"),
  previewFrame: document.getElementById("previewFrame"),
  previewImage: document.getElementById("previewImage"),
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
  debugDiffs: document.getElementById("debugDiffs"),
  saveCleaned: document.getElementById("saveCleaned"),
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

function applySource(source) {
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
    els.startInput.value = "0";
  }

  updateCropUi();
}

async function loadPreview(timeValue) {
  if (!state.sourceId) return;
  const time = Number.isFinite(Number(timeValue)) ? Number(timeValue) : 0;
  els.refreshPreview.disabled = true;
  setStatus(els.importStatus, "Loading preview...");
  try {
    const data = await fetchJson("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: state.sourceId, time }),
    });
    els.previewImage.src = `${data.preview_url}?v=${Date.now()}`;
    setStatus(els.importStatus, "");
  } catch (error) {
    setStatus(els.importStatus, error.message, "error");
  } finally {
    els.refreshPreview.disabled = false;
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
    debug_diffs: els.debugDiffs.checked,
    save_cleaned: els.saveCleaned.checked,
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
  setStatus(els.importStatus, "Importing...");
  const formData = new FormData(els.importForm);
  try {
    const source = await fetchJson("/api/import", {
      method: "POST",
      body: formData,
    });
    applySource(source);
    setStatus(els.importStatus, "Imported.", "success");
    await loadStartPreview();
  } catch (error) {
    setStatus(els.importStatus, error.message, "error");
  }
});

els.refreshPreview.addEventListener("click", () => {
  loadStartPreview();
});

els.startInput.addEventListener("change", () => {
  loadStartPreview();
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
