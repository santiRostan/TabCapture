import contextlib
import threading
import time
import uuid
import os
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from tab_extractor import (
    ExtractionOptions,
    ExtractionStats,
    VideoMetadata,
    build_pdf_from_images,
    build_video_metadata,
    download_video,
    extract_unique_crops,
    get_video_duration,
    probe_youtube_metadata,
    save_video_frame,
    validate_crop_ratios,
)


ROOT_DIR = Path(__file__).parent.resolve()
CACHE_DIR = ROOT_DIR / "app_cache"
UPLOADS_DIR = CACHE_DIR / "uploads"
PREVIEWS_DIR = CACHE_DIR / "previews"
RUNS_DIR = CACHE_DIR / "runs"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
YOUTUBE_PREVIEW_MAX_HEIGHT = 480

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

SOURCES: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
STORE_LOCK = threading.Lock()


def ensure_cache_dirs() -> None:
    for directory in (UPLOADS_DIR, PREVIEWS_DIR, RUNS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def is_youtube_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def metadata_to_dict(metadata: VideoMetadata) -> Dict[str, Optional[str]]:
    return {
        "raw_title": metadata.raw_title,
        "display_title": metadata.display_title,
        "channel": metadata.channel,
        "source_url": metadata.source_url,
    }


def metadata_from_dict(data: Dict[str, Any]) -> VideoMetadata:
    return VideoMetadata(
        raw_title=data.get("raw_title"),
        display_title=data.get("display_title"),
        channel=data.get("channel"),
        source_url=data.get("source_url"),
    )


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def parse_float(value: Any, field_name: str, default: Optional[float] = None) -> float:
    if value in (None, ""):
        if default is None:
            raise ValueError(f"{field_name} is required")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def parse_optional_float(value: Any, field_name: str) -> Optional[float]:
    if value in (None, ""):
        return None
    return parse_float(value, field_name)


def parse_bool(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes", "on"}


def append_job_log(job_id: str, text: str) -> None:
    text = text.replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return
    with STORE_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"].extend(lines)
        job["logs"] = job["logs"][-300:]


def update_job(job_id: str, **updates: Any) -> None:
    with STORE_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(updates)


class JobLogWriter:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def write(self, text: str) -> int:
        append_job_log(self.job_id, text)
        return len(text)

    def flush(self) -> None:
        pass


def make_progress_callback(job_id: str):
    def progress(phase: str, stats: ExtractionStats) -> None:
        update_job(job_id, phase=phase, stats=stats.to_dict(), updated_at=time.time())

    return progress


def source_or_error(source_id: str) -> Dict[str, Any]:
    with STORE_LOCK:
        source = SOURCES.get(source_id)
    if source is None:
        raise ValueError("Unknown source. Import a video first.")
    return source


def safe_pdf_name(value: str) -> str:
    filename = secure_filename(value or "tablatura.pdf")
    if not filename:
        filename = "tablatura.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def cached_youtube_preview_video(source: Dict[str, Any]) -> Path:
    preview_dir = PREVIEWS_DIR / source["id"] / "video"
    preview_dir.mkdir(parents=True, exist_ok=True)

    candidates = []
    for ext in ALLOWED_VIDEO_EXTENSIONS:
        candidates.extend(preview_dir.glob(f"video*{ext}"))
    candidates = [path for path in candidates if path.is_file() and path.stat().st_size > 0]
    if candidates:
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0]

    video_path, _metadata, _downloaded_start = download_video(
        source["url"],
        preview_dir,
        max_height=YOUTUBE_PREVIEW_MAX_HEIGHT,
    )
    return video_path


def run_extraction_job(job_id: str, payload: Dict[str, Any]) -> None:
    log_writer = JobLogWriter(job_id)
    with contextlib.redirect_stdout(log_writer), contextlib.redirect_stderr(log_writer):
        try:
            source = source_or_error(payload["source_id"])
            start_sec = parse_float(payload.get("start"), "start", 0.0)
            end_sec = parse_optional_float(payload.get("end"), "end")
            if start_sec < 0:
                raise ValueError("start must be 0 or greater")
            if end_sec is not None and end_sec <= start_sec:
                raise ValueError("end must be greater than start")

            crop_y_start = parse_float(payload.get("crop_y_start"), "crop_y_start", 0.0)
            crop_y_end = parse_float(payload.get("crop_y_end"), "crop_y_end", 0.46)
            validate_crop_ratios(crop_y_start, crop_y_end)

            options = ExtractionOptions(
                sample_every_sec=parse_float(payload.get("sample_every"), "sample_every", 2.0),
                crop_y_start_ratio=crop_y_start,
                crop_y_end_ratio=crop_y_end,
                diff_threshold=parse_float(payload.get("diff_threshold"), "diff_threshold", 0.010),
                compare_window=int(parse_float(payload.get("compare_window"), "compare_window", 1)),
                debug_diffs=parse_bool(payload.get("debug_diffs")),
                start_sec=start_sec,
                end_sec=end_sec,
                save_cleaned=parse_bool(payload.get("save_cleaned")),
                band_half_width=int(parse_float(payload.get("band_half_width"), "band_half_width", 90)),
                min_band_pixels=int(parse_float(payload.get("min_band_pixels"), "min_band_pixels", 20)),
                target_tolerance=int(parse_float(payload.get("target_tolerance"), "target_tolerance", 48)),
            )

            run_dir = RUNS_DIR / job_id
            download_dir = run_dir / "download"
            crops_dir = run_dir / "crops"
            comparison_dir = run_dir / "comparison" if options.debug_diffs else None
            run_dir.mkdir(parents=True, exist_ok=True)

            update_job(job_id, status="running", phase="downloading", updated_at=time.time())

            source_metadata = metadata_from_dict(source["metadata"])
            if source["type"] == "youtube":
                print("Downloading YouTube video...")
                video_path, downloaded_metadata, downloaded_start_sec = download_video(
                    source["url"],
                    download_dir,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
                metadata = build_video_metadata(
                    raw_title=downloaded_metadata.raw_title,
                    channel=downloaded_metadata.channel,
                    source_url=downloaded_metadata.source_url,
                    title_override=payload.get("title"),
                    channel_override=payload.get("channel"),
                )
            else:
                video_path = Path(source["path"])
                downloaded_start_sec = 0.0
                metadata = build_video_metadata(
                    raw_title=source_metadata.raw_title,
                    channel=source_metadata.channel,
                    source_url=None,
                    title_override=payload.get("title"),
                    channel_override=payload.get("channel"),
                )

            extract_start_sec = max(0.0, options.start_sec - downloaded_start_sec)
            extract_end_sec = (
                options.end_sec - downloaded_start_sec
                if options.end_sec is not None
                else None
            )

            update_job(job_id, phase="extracting", updated_at=time.time())
            stats = extract_unique_crops(
                video_path=video_path,
                crops_dir=crops_dir,
                comparison_dir=comparison_dir,
                sample_every_sec=options.sample_every_sec,
                crop_y_start_ratio=options.crop_y_start_ratio,
                crop_y_end_ratio=options.crop_y_end_ratio,
                hash_threshold=options.hash_threshold,
                hash_size=options.hash_size,
                diff_threshold=options.diff_threshold,
                compare_window=options.compare_window,
                debug_diffs=options.debug_diffs,
                start_sec=extract_start_sec,
                end_sec=extract_end_sec,
                save_cleaned=options.save_cleaned,
                band_half_width=options.band_half_width,
                min_band_pixels=options.min_band_pixels,
                target_tolerance=options.target_tolerance,
                progress_callback=make_progress_callback(job_id),
            )
            if stats.captures_kept == 0:
                raise RuntimeError("No useful captures were extracted.")

            update_job(job_id, phase="building_pdf", updated_at=time.time())
            output_pdf = run_dir / safe_pdf_name(payload.get("output") or "tablatura.pdf")
            build_pdf_from_images(crops_dir, output_pdf, metadata=metadata)

            update_job(
                job_id,
                status="done",
                phase="done",
                stats=stats.to_dict(),
                pdf_url=f"/api/jobs/{job_id}/pdf",
                pdf_name=output_pdf.name,
                updated_at=time.time(),
            )
            print(f"PDF generated: {output_pdf}")
        except Exception as exc:
            update_job(
                job_id,
                status="error",
                phase="error",
                error=str(exc),
                updated_at=time.time(),
            )
            print(f"ERROR: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/import")
def import_source():
    ensure_cache_dirs()
    url = (request.form.get("url") or "").strip()
    upload = request.files.get("file")

    try:
        if url:
            if not is_youtube_url(url):
                return json_error("Enter a valid YouTube URL.")
            metadata, duration = probe_youtube_metadata(url)
            source_id = uuid.uuid4().hex
            source = {
                "id": source_id,
                "type": "youtube",
                "url": url,
                "metadata": metadata_to_dict(metadata),
                "duration": duration,
            }
        elif upload and upload.filename:
            original_name = secure_filename(upload.filename)
            suffix = Path(original_name).suffix.lower()
            if suffix not in ALLOWED_VIDEO_EXTENSIONS:
                return json_error("Unsupported video type. Use mp4, mov, mkv, or webm.")
            source_id = uuid.uuid4().hex
            upload_dir = UPLOADS_DIR / source_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            video_path = upload_dir / original_name
            upload.save(video_path)
            duration = get_video_duration(video_path)
            metadata = build_video_metadata(raw_title=Path(original_name).stem)
            source = {
                "id": source_id,
                "type": "local",
                "path": str(video_path),
                "metadata": metadata_to_dict(metadata),
                "duration": duration,
            }
        else:
            return json_error("Add a YouTube URL or choose a local video.")

        with STORE_LOCK:
            SOURCES[source_id] = source

        return jsonify(source)
    except Exception as exc:
        return json_error(str(exc), 500)


@app.post("/api/preview")
def preview_source():
    ensure_cache_dirs()
    data = request.get_json(force=True, silent=True) or {}
    try:
        source = source_or_error(data.get("source_id"))
        time_sec = parse_float(data.get("time"), "time", 0.0)
        if time_sec < 0:
            raise ValueError("time must be 0 or greater")

        preview_name = f"{source['id']}_{int(time_sec * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
        preview_path = PREVIEWS_DIR / preview_name

        if source["type"] == "youtube":
            video_path = cached_youtube_preview_video(source)
            save_video_frame(video_path, preview_path, time_sec=time_sec)
        else:
            save_video_frame(Path(source["path"]), preview_path, time_sec=time_sec)

        return jsonify(
            {
                "preview_url": f"/api/previews/{preview_name}",
                "duration": source.get("duration"),
            }
        )
    except Exception as exc:
        return json_error(str(exc), 500)


@app.post("/api/extract")
def start_extract():
    ensure_cache_dirs()
    payload = request.get_json(force=True, silent=True) or {}
    try:
        source_or_error(payload.get("source_id"))
        start_sec = parse_float(payload.get("start"), "start", 0.0)
        end_sec = parse_optional_float(payload.get("end"), "end")
        if start_sec < 0:
            return json_error("Start must be 0 or greater.")
        if end_sec is not None and end_sec <= start_sec:
            return json_error("End must be greater than start.")
        validate_crop_ratios(
            parse_float(payload.get("crop_y_start"), "crop_y_start", 0.0),
            parse_float(payload.get("crop_y_end"), "crop_y_end", 0.46),
        )

        job_id = uuid.uuid4().hex
        with STORE_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "logs": [],
                "stats": None,
                "error": None,
                "pdf_url": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        thread = threading.Thread(
            target=run_extraction_job,
            args=(job_id, payload),
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job_id})
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with STORE_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job = dict(job)
            job["logs"] = list(job.get("logs", []))
    if job is None:
        return json_error("Unknown job.", 404)
    return jsonify(job)


@app.get("/api/jobs/<job_id>/pdf")
def job_pdf(job_id: str):
    with STORE_LOCK:
        job = JOBS.get(job_id)
    if job is None or job.get("status") != "done":
        return json_error("PDF is not ready.", 404)
    return send_from_directory(RUNS_DIR / job_id, job["pdf_name"], as_attachment=False)


@app.get("/api/previews/<path:filename>")
def preview_file(filename: str):
    return send_from_directory(PREVIEWS_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    ensure_cache_dirs()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
