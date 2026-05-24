import argparse
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None


# Compatibilidad con distintas versiones de Pillow
if hasattr(Image, "Resampling"):
    RESAMPLE = Image.Resampling.LANCZOS
else:
    RESAMPLE = Image.LANCZOS


@dataclass
class VideoMetadata:
    raw_title: Optional[str] = None
    display_title: Optional[str] = None
    channel: Optional[str] = None
    source_url: Optional[str] = None


@dataclass
class ExtractionOptions:
    sample_every_sec: float = 2.0
    crop_y_start_ratio: float = 0.0
    crop_y_end_ratio: float = 0.46
    hash_threshold: int = 16
    hash_size: int = 12
    diff_threshold: float = 0.010
    compare_window: int = 1
    debug_diffs: bool = False
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    save_cleaned: bool = False
    band_half_width: int = 90
    min_band_pixels: int = 20
    target_tolerance: int = 48


@dataclass
class ExtractionStats:
    duration: float = 0.0
    start_sec: float = 0.0
    end_sec: float = 0.0
    frames_checked: int = 0
    duplicates_skipped: int = 0
    captures_kept: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duration": self.duration,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "frames_checked": self.frames_checked,
            "duplicates_skipped": self.duplicates_skipped,
            "captures_kept": self.captures_kept,
        }


ProgressCallback = Callable[[str, ExtractionStats], None]


def clean_video_title(raw_title: Optional[str]) -> Optional[str]:
    if not raw_title:
        return None

    original = raw_title.strip()
    title = original

    title = re.sub(r"^\([^)]{1,100}\)\s*", "", title).strip()

    parts = re.split(r"\s+-\s+", title)
    if len(parts) > 1:
        suffix = " - ".join(parts[1:]).lower()
        descriptor_keywords = (
            "tab",
            "tabs",
            "lesson",
            "tutorial",
            "guitar",
            "cover",
            "sheet",
            "chord",
        )
        if any(keyword in suffix for keyword in descriptor_keywords):
            title = parts[0].strip()

    return title or original or None


def build_video_metadata(
    raw_title: Optional[str] = None,
    channel: Optional[str] = None,
    source_url: Optional[str] = None,
    title_override: Optional[str] = None,
    channel_override: Optional[str] = None,
) -> VideoMetadata:
    title_override = title_override.strip() if title_override else None
    channel_override = channel_override.strip() if channel_override else None
    raw_title = raw_title.strip() if raw_title else None
    channel = channel.strip() if channel else None

    return VideoMetadata(
        raw_title=raw_title,
        display_title=title_override or clean_video_title(raw_title),
        channel=channel_override or channel,
        source_url=source_url,
    )


def youtube_ydl_base_opts(quiet: bool = False) -> Dict[str, Any]:
    return {
        "quiet": quiet,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            }
        },
    }


def youtube_video_format(max_height: int = 1080) -> str:
    return (
        f"bestvideo[height<={max_height}][ext=mp4][vcodec^=avc1]/"
        f"bestvideo[height<={max_height}][vcodec^=avc1]/"
        f"bestvideo[height<={max_height}]/"
        f"best[height<={max_height}][ext=mp4]/"
        f"best[height<={max_height}]/"
        "best[ext=mp4]/best"
    )


def metadata_from_yt_info(info: Dict[str, Any], fallback_url: str) -> VideoMetadata:
    return build_video_metadata(
        raw_title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        source_url=info.get("webpage_url") or fallback_url,
    )


def probe_youtube_metadata(url: str) -> Tuple[VideoMetadata, Optional[float]]:
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp no está instalado. Ejecutá: pip install -r requirements.txt")

    ydl_opts = youtube_ydl_base_opts(quiet=True)
    ydl_opts["skip_download"] = True

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    return metadata_from_yt_info(info, url), info.get("duration")


def download_video(
    url: str,
    output_dir: Path,
    start_sec: float = 0.0,
    end_sec: Optional[float] = None,
    max_height: int = 1080,
) -> Tuple[Path, VideoMetadata, float]:
    """
    Descarga un video desde YouTube usando yt-dlp.

    Importante:
    Como solo necesitamos frames, bajamos video sin audio. El recorte se hace
    luego sobre el archivo local porque los range-downloads de YouTube pueden
    quedarse colgados o devolver 403 en URLs de googlevideo.
    """
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp no está instalado. Ejecutá: pip install -r requirements.txt")

    output_template = str(output_dir / "video.%(ext)s")

    ydl_opts = youtube_ydl_base_opts(quiet=False)
    ydl_opts.update({
        "format": youtube_video_format(max_height=max_height),
        "outtmpl": output_template,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    })

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    candidates = []
    for ext in ("mp4", "mkv", "webm", "mov"):
        candidates.extend(output_dir.glob(f"video*.{ext}"))

    if not candidates:
        raise FileNotFoundError("No se encontró el video descargado.")

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    metadata = metadata_from_yt_info(info, url)

    return candidates[0], metadata, 0.0


def dhash(image: Image.Image, hash_size: int = 12) -> int:
    """
    Difference hash para detectar imágenes similares.
    hash_size=12 da un hash de 144 bits, más estable que el clásico 8x8.
    """
    gray = ImageOps.grayscale(image)
    resized = gray.resize((hash_size + 1, hash_size), RESAMPLE)
    pixels = np.array(resized)

    diff = pixels[:, 1:] > pixels[:, :-1]

    h = 0
    for bit in diff.flatten():
        h = (h << 1) | int(bit)

    return h


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def is_mostly_dark_or_blank(image: Image.Image, std_threshold: float = 8.0) -> bool:
    """
    Filtra imágenes casi vacías o muy uniformes.
    """
    gray = ImageOps.grayscale(image)
    stat = ImageStat.Stat(gray)
    stddev = stat.stddev[0]
    return stddev < std_threshold


def create_blue_green_mask(
    image: Image.Image,
    target_rgb: Tuple[int, int, int] = (204, 216, 240),  # #ccd8f0
    target_tolerance: int = 48,
) -> np.ndarray:
    """
    Crea una máscara de píxeles que probablemente pertenezcan a la barra:
    - el color exacto aproximado #ccd8f0
    - tonos azules/celestes
    - tonos verdes

    Devuelve una máscara uint8 con valores 0 o 255.
    """
    rgb = np.array(image.convert("RGB"), dtype=np.uint8)

    # 1) Detección por cercanía al color #ccd8f0
    rgb_i32 = rgb.astype(np.int32)
    target = np.array(target_rgb, dtype=np.int32)

    diff = rgb_i32 - target
    dist_sq = np.sum(diff * diff, axis=2)
    target_mask = (dist_sq <= target_tolerance * target_tolerance).astype(np.uint8) * 255

    # 2) Detección general por HSV: azul/celeste/verde
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)

    # OpenCV HSV:
    # H va de 0 a 179.
    # Estos rangos son deliberadamente amplios porque YouTube comprime y cambia tonos.
    blue_lower = np.array([80, 20, 50])
    blue_upper = np.array([145, 255, 255])
    blue_mask = cv2.inRange(hsv, blue_lower, blue_upper)

    green_lower = np.array([35, 20, 40])
    green_upper = np.array([90, 255, 255])
    green_mask = cv2.inRange(hsv, green_lower, green_upper)

    mask = cv2.bitwise_or(target_mask, blue_mask)
    mask = cv2.bitwise_or(mask, green_mask)

    return mask


def detect_vertical_playhead_band(
    color_mask: np.ndarray,
    min_band_pixels: int = 20,
    smooth_width: int = 31,
) -> Optional[Tuple[int, float]]:
    """
    Detecta la columna x donde probablemente está la franja vertical.

    En vez de borrar todos los píxeles azules/verdes sin pensar,
    miramos en qué columna se concentra más ese color.
    """
    if color_mask.ndim != 2:
        raise ValueError("color_mask debe ser una imagen en escala de grises.")

    h, w = color_mask.shape

    # Cantidad de píxeles detectados por columna
    col_scores = np.sum(color_mask > 0, axis=0).astype(np.float32)

    if smooth_width < 3:
        smooth_width = 3

    if smooth_width % 2 == 0:
        smooth_width += 1

    smooth_width = min(smooth_width, max(3, w // 2))

    kernel = np.ones(smooth_width, dtype=np.float32) / smooth_width
    smoothed = np.convolve(col_scores, kernel, mode="same")

    center_x = int(np.argmax(smoothed))
    peak_score = float(smoothed[center_x])

    if peak_score < min_band_pixels:
        return None

    return center_x, peak_score


def remove_playhead_for_comparison(
    image: Image.Image,
    band_half_width: int = 60,
    min_band_pixels: int = 20,
    target_tolerance: int = 48,
    remove_color_pixels_too: bool = True,
) -> Tuple[Image.Image, Optional[int], float]:
    """
    Devuelve una versión de la imagen con la franja vertical ignorada.

    Estrategia:
    1. Detecta píxeles azules/verdes/#ccd8f0.
    2. Busca la columna donde más se concentran.
    3. Borra una franja vertical completa alrededor.
    4. Como fallback, también puede borrar píxeles azules/verdes sueltos.

    Esto se usa para comparar duplicados, no necesariamente para guardar el PDF.
    """
    rgb = np.array(image.convert("RGB"), dtype=np.uint8)

    color_mask = create_blue_green_mask(
        image,
        target_rgb=(204, 216, 240),
        target_tolerance=target_tolerance,
    )

    # Suavizamos un poco la máscara para unir bordes de la barra/playhead
    small_kernel = np.ones((3, 3), np.uint8)
    color_mask = cv2.dilate(color_mask, small_kernel, iterations=1)

    detected = detect_vertical_playhead_band(
        color_mask,
        min_band_pixels=min_band_pixels,
        smooth_width=31,
    )

    h, w, _ = rgb.shape
    removal_mask = np.zeros((h, w), dtype=np.uint8)

    band_center = None
    peak_score = 0.0

    if detected is not None:
        band_center, peak_score = detected

        x1 = max(0, band_center - band_half_width)
        x2 = min(w, band_center + band_half_width)

        # Borramos toda la franja vertical alrededor del playhead.
        removal_mask[:, x1:x2] = 255

    if remove_color_pixels_too:
        # Fallback: también borramos restos azules/verdes fuera de la franja.
        # Útil cuando las notas que están sonando cambian de color.
        color_cleanup_kernel = np.ones((5, 5), np.uint8)
        expanded_color_mask = cv2.dilate(color_mask, color_cleanup_kernel, iterations=1)
        removal_mask = cv2.bitwise_or(removal_mask, expanded_color_mask)

    cleaned = rgb.copy()
    cleaned[removal_mask > 0] = [255, 255, 255]

    return Image.fromarray(cleaned), band_center, peak_score


@dataclass
class ComparisonFrame:
    original: Image.Image
    normalized: Image.Image
    band_center: Optional[int]
    peak_score: float
    time_sec: float


def normalize_tab_for_hash(image: Image.Image) -> Image.Image:
    """
    Normaliza la imagen para comparación:
    - escala de grises
    - binarización suave

    Esto reduce diferencias menores por compresión del video.
    """
    gray = ImageOps.grayscale(image)
    arr = np.array(gray)

    # Threshold: fondo claro queda blanco, líneas/texto quedan negro.
    arr = np.where(arr < 185, 0, 255).astype(np.uint8)

    return Image.fromarray(arr)


def detect_playhead_in_image(
    image: Image.Image,
    min_band_pixels: int = 20,
    target_tolerance: int = 48,
) -> Tuple[Optional[int], float]:
    """
    Detecta la columna central del playhead sin borrar píxeles de color sueltos.
    """
    color_mask = create_blue_green_mask(
        image,
        target_rgb=(204, 216, 240),
        target_tolerance=target_tolerance,
    )

    small_kernel = np.ones((3, 3), np.uint8)
    color_mask = cv2.dilate(color_mask, small_kernel, iterations=1)

    detected = detect_vertical_playhead_band(
        color_mask,
        min_band_pixels=min_band_pixels,
        smooth_width=31,
    )

    if detected is None:
        return None, 0.0

    return detected


def build_playhead_mask(
    shape: Tuple[int, int],
    centers: List[Optional[int]],
    band_half_width: int,
) -> np.ndarray:
    """
    Construye una máscara única para ignorar las bandas de ambos frames.
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)

    for center in centers:
        if center is None:
            continue

        x1 = max(0, center - band_half_width)
        x2 = min(w, center + band_half_width)
        mask[:, x1:x2] = True

    return mask


def apply_ignore_mask(image: Image.Image, mask: np.ndarray) -> Image.Image:
    arr = np.array(image.convert("L"), dtype=np.uint8)
    arr[mask] = 255
    return Image.fromarray(arr)


def prepare_comparison_frame(
    original_img: Image.Image,
    time_sec: float,
    min_band_pixels: int,
    target_tolerance: int,
) -> ComparisonFrame:
    band_center, peak_score = detect_playhead_in_image(
        original_img,
        min_band_pixels=min_band_pixels,
        target_tolerance=target_tolerance,
    )
    normalized = normalize_tab_for_hash(original_img)

    return ComparisonFrame(
        original=original_img,
        normalized=normalized,
        band_center=band_center,
        peak_score=peak_score,
        time_sec=time_sec,
    )


def masked_diff_ratio(
    previous: ComparisonFrame,
    current: ComparisonFrame,
    band_half_width: int,
) -> Tuple[float, np.ndarray, Image.Image, Image.Image, Image.Image]:
    """
    Compara dos frames usando la misma máscara de playhead para ambos.
    """
    prev_arr = np.array(previous.normalized.convert("L"), dtype=np.uint8)
    curr_arr = np.array(current.normalized.convert("L"), dtype=np.uint8)

    if prev_arr.shape != curr_arr.shape:
        raise ValueError("Las imágenes de comparación deben tener el mismo tamaño.")

    ignore_mask = build_playhead_mask(
        prev_arr.shape,
        [previous.band_center, current.band_center],
        band_half_width=band_half_width,
    )
    compare_mask = ~ignore_mask

    if not np.any(compare_mask):
        return 1.0, ignore_mask, previous.normalized, current.normalized, current.normalized

    changed = (prev_arr != curr_arr) & compare_mask
    diff_ratio = float(np.count_nonzero(changed) / np.count_nonzero(compare_mask))

    prev_masked = prev_arr.copy()
    curr_masked = curr_arr.copy()
    prev_masked[ignore_mask] = 255
    curr_masked[ignore_mask] = 255

    diff_vis = np.full(curr_arr.shape, 255, dtype=np.uint8)
    diff_vis[changed] = 0

    return (
        diff_ratio,
        ignore_mask,
        Image.fromarray(prev_masked),
        Image.fromarray(curr_masked),
        Image.fromarray(diff_vis),
    )


def save_debug_images(
    comparison_dir: Path,
    index: int,
    current: ComparisonFrame,
    decision: str,
    masked_current: Optional[Image.Image] = None,
    diff_image: Optional[Image.Image] = None,
):
    safe_decision = decision.replace(" ", "_")
    prefix = f"{index:04d}_t{int(current.time_sec):05d}_{safe_decision}"

    current.normalized.convert("RGB").save(comparison_dir / f"{prefix}_normalized.png")

    if masked_current is not None:
        masked_current.convert("RGB").save(comparison_dir / f"{prefix}_masked.png")

    if diff_image is not None:
        diff_image.convert("RGB").save(comparison_dir / f"{prefix}_diff.png")


def mask_playhead_in_original(
    image: Image.Image,
    band_center: Optional[int],
    band_half_width: int,
) -> Image.Image:
    arr = np.array(image.convert("RGB"), dtype=np.uint8)
    h, w, _ = arr.shape
    mask = build_playhead_mask((h, w), [band_center], band_half_width)
    arr[mask] = [255, 255, 255]
    return Image.fromarray(arr)


def validate_crop_ratios(crop_y_start_ratio: float, crop_y_end_ratio: float) -> None:
    if not 0.0 <= crop_y_start_ratio < crop_y_end_ratio <= 1.0:
        raise ValueError(
            "El recorte debe cumplir 0 <= crop_y_start < crop_y_end <= 1."
        )
    if crop_y_end_ratio - crop_y_start_ratio < 0.05:
        raise ValueError("El recorte vertical debe cubrir al menos 5% del video.")


def crop_frame_rgb(
    frame_rgb: np.ndarray,
    crop_y_start_ratio: float,
    crop_y_end_ratio: float,
) -> np.ndarray:
    validate_crop_ratios(crop_y_start_ratio, crop_y_end_ratio)
    h, _w, _ = frame_rgb.shape
    y1 = int(round(h * crop_y_start_ratio))
    y2 = int(round(h * crop_y_end_ratio))
    y1 = max(0, min(h - 1, y1))
    y2 = max(y1 + 1, min(h, y2))
    return frame_rgb[y1:y2, :, :]


def save_video_frame(
    video_path: Path,
    output_path: Path,
    time_sec: float = 0.0,
    max_width: int = 1280,
) -> Path:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir el video: {video_path}")

    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, time_sec) * 1000)
    ok, frame = cap.read()
    cap.release()

    if not ok:
        raise RuntimeError(f"No se pudo leer un frame en t={time_sec:.2f}s")

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(frame_rgb)

    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), RESAMPLE)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_path, quality=90)
    return output_path


def get_video_duration(video_path: Path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir el video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return frame_count / fps if fps > 0 else 0.0


def extract_unique_crops(
    video_path: Path,
    crops_dir: Path,
    comparison_dir: Optional[Path] = None,
    sample_every_sec: float = 2.0,
    crop_top_ratio: Optional[float] = 0.46,
    crop_y_start_ratio: Optional[float] = None,
    crop_y_end_ratio: Optional[float] = None,
    hash_threshold: int = 16,
    hash_size: int = 12,
    diff_threshold: float = 0.010,
    compare_window: int = 1,
    debug_diffs: bool = False,
    start_sec: float = 0.0,
    end_sec: Optional[float] = None,
    save_cleaned: bool = False,
    band_half_width: int = 90,
    min_band_pixels: int = 20,
    target_tolerance: int = 48,
    progress_callback: Optional[ProgressCallback] = None,
) -> ExtractionStats:
    """
    Extrae capturas cada X segundos, recorta la parte superior,
    elimina duplicados ignorando la franja vertical azul/verde móvil,
    y guarda las imágenes finales.
    """
    if crop_y_start_ratio is None:
        crop_y_start_ratio = 0.0
    if crop_y_end_ratio is None:
        crop_y_end_ratio = crop_top_ratio if crop_top_ratio is not None else 0.46
    validate_crop_ratios(crop_y_start_ratio, crop_y_end_ratio)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir el video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if fps > 0 else 0

    if end_sec is None or end_sec > duration:
        end_sec = duration

    crops_dir.mkdir(parents=True, exist_ok=True)

    if comparison_dir is not None:
        comparison_dir.mkdir(parents=True, exist_ok=True)

    current_time = start_sec
    stats = ExtractionStats(
        duration=duration,
        start_sec=start_sec,
        end_sec=end_sec,
    )
    recent_kept: List[ComparisonFrame] = []
    compare_window = max(1, compare_window)

    print(f"Duración detectada: {duration:.2f}s")
    print(f"Procesando desde {start_sec:.2f}s hasta {end_sec:.2f}s")
    print(f"Capturando cada {sample_every_sec:.2f}s")
    print(
        f"diff_threshold={diff_threshold:.4f}, "
        f"compare_window={compare_window}, band_half_width={band_half_width}"
    )
    print(
        f"crop_y_start={crop_y_start_ratio:.3f}, "
        f"crop_y_end={crop_y_end_ratio:.3f}"
    )
    if progress_callback is not None:
        progress_callback("started", stats)

    while current_time <= end_sec:
        cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
        ok, frame = cap.read()
        if not ok:
            break

        stats.frames_checked += 1

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        cropped = crop_frame_rgb(frame_rgb, crop_y_start_ratio, crop_y_end_ratio)

        original_img = Image.fromarray(cropped)

        if is_mostly_dark_or_blank(original_img):
            current_time += sample_every_sec
            continue

        current_comparison = prepare_comparison_frame(
            original_img,
            time_sec=current_time,
            min_band_pixels=min_band_pixels,
            target_tolerance=target_tolerance,
        )

        best_diff = None
        best_masked_current = None
        best_diff_image = None
        is_duplicate = False

        for previous_comparison in recent_kept[-compare_window:]:
            (
                diff_ratio,
                _ignore_mask,
                _masked_previous,
                masked_current,
                diff_image,
            ) = masked_diff_ratio(
                previous_comparison,
                current_comparison,
                band_half_width=band_half_width,
            )

            if best_diff is None or diff_ratio < best_diff:
                best_diff = diff_ratio
                best_masked_current = masked_current
                best_diff_image = diff_image

            if diff_ratio <= diff_threshold:
                is_duplicate = True
                break

        if is_duplicate:
            stats.duplicates_skipped += 1
            if comparison_dir is not None and debug_diffs:
                save_debug_images(
                    comparison_dir,
                    stats.frames_checked,
                    current_comparison,
                    decision=f"duplicate_diff_{best_diff:.4f}",
                    masked_current=best_masked_current,
                    diff_image=best_diff_image,
                )
            if progress_callback is not None:
                progress_callback("frame", stats)
            current_time += sample_every_sec
            continue

        output_path = crops_dir / (
            f"crop_{stats.captures_kept:04d}_t{int(current_time):05d}.png"
        )

        if save_cleaned:
            cleaned_img = mask_playhead_in_original(
                original_img,
                current_comparison.band_center,
                band_half_width=band_half_width,
            )
            cleaned_img.convert("RGB").save(output_path)
        else:
            original_img.convert("RGB").save(output_path)

        if comparison_dir is not None:
            save_debug_images(
                comparison_dir,
                stats.frames_checked,
                current_comparison,
                decision=(
                    f"kept_diff_{best_diff:.4f}"
                    if best_diff is not None
                    else "kept_first"
                ),
                masked_current=best_masked_current,
                diff_image=best_diff_image if debug_diffs else None,
            )

        stats.captures_kept += 1
        recent_kept.append(current_comparison)

        band_info = "sin banda detectada"
        if current_comparison.band_center is not None:
            band_info = (
                f"banda x={current_comparison.band_center}, "
                f"peak={current_comparison.peak_score:.1f}"
            )

        diff_info = "diff=inicial"
        if best_diff is not None:
            diff_info = f"diff={best_diff:.4f}"

        print(
            f"[{stats.captures_kept:03d}] guardada t={current_time:.2f}s | "
            f"{diff_info} | {band_info}"
        )
        if progress_callback is not None:
            progress_callback("frame", stats)

        current_time += sample_every_sec

    cap.release()

    print(f"Frames chequeados: {stats.frames_checked}")
    print(f"Duplicados saltados: {stats.duplicates_skipped}")
    print(f"Capturas finales: {stats.captures_kept}")
    if progress_callback is not None:
        progress_callback("finished", stats)

    return stats


def load_font(size: int):
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/Library/Fonts/Arial.ttf",
        windows_fonts / "arial.ttf",
        windows_fonts / "segoeui.ttf",
        windows_fonts / "calibri.ttf",
        windows_fonts / "verdana.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "arial.ttf",
        "Arial.ttf",
        "DejaVuSans.ttf",
    ]

    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue

    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
) -> List[str]:
    words = text.split()
    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:
        candidate = f"{current} {word}"
        candidate_w, _ = text_size(draw, candidate, font)
        if candidate_w <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def create_cover_page(
    metadata: VideoMetadata,
    page_width: int,
    page_height: int,
    bg_color: str = "white",
) -> Image.Image:
    page = Image.new("RGB", (page_width, page_height), bg_color)
    draw = ImageDraw.Draw(page)

    title_font = load_font(78)
    channel_font = load_font(38)
    source_font = load_font(24)

    text_color = (24, 24, 24)
    muted_color = (95, 95, 95)
    max_text_width = int(page_width * 0.78)

    title = metadata.display_title or "Extracted tablature"
    title_lines = wrap_text(draw, title, title_font, max_text_width)

    title_line_heights = [text_size(draw, line, title_font)[1] for line in title_lines]
    title_block_height = sum(title_line_heights) + max(0, len(title_lines) - 1) * 20
    channel_text = f"From: {metadata.channel}" if metadata.channel else None
    channel_h = text_size(draw, channel_text, channel_font)[1] if channel_text else 0

    total_block_height = title_block_height + (52 + channel_h if channel_text else 0)
    current_y = int((page_height - total_block_height) * 0.45)

    for idx, line in enumerate(title_lines):
        line_w, line_h = text_size(draw, line, title_font)
        draw.text(
            ((page_width - line_w) / 2, current_y),
            line,
            fill=text_color,
            font=title_font,
        )
        current_y += line_h + (20 if idx < len(title_lines) - 1 else 0)

    if channel_text:
        current_y += 52
        channel_w, _ = text_size(draw, channel_text, channel_font)
        draw.text(
            ((page_width - channel_w) / 2, current_y),
            channel_text,
            fill=muted_color,
            font=channel_font,
        )

    if metadata.source_url:
        source_lines = wrap_text(draw, metadata.source_url, source_font, max_text_width)
        source_y = page_height - 180
        for line in source_lines[:2]:
            line_w, line_h = text_size(draw, line, source_font)
            draw.text(
                ((page_width - line_w) / 2, source_y),
                line,
                fill=muted_color,
                font=source_font,
            )
            source_y += line_h + 10

    return page


def build_pdf_from_images(
    images_dir: Path,
    output_pdf: Path,
    metadata: Optional[VideoMetadata] = None,
    page_width: int = 1654,   # aprox A4 a ~150 dpi
    page_height: int = 2339,
    margin: int = 40,
    spacing: int = 25,
    bg_color: str = "white",
):
    """
    Crea un PDF con varias capturas por página, una debajo de la otra.
    """
    image_paths = sorted(images_dir.glob("*.png"))
    if not image_paths:
        raise RuntimeError("No hay imágenes para armar el PDF.")

    pages = []

    if metadata is not None and (metadata.display_title or metadata.channel):
        pages.append(
            create_cover_page(
                metadata,
                page_width=page_width,
                page_height=page_height,
                bg_color=bg_color,
            )
        )

    current_y = margin
    page = Image.new("RGB", (page_width, page_height), bg_color)

    for img_path in image_paths:
        img = Image.open(img_path).convert("RGB")

        max_w = page_width - 2 * margin
        ratio = max_w / img.width
        new_w = int(img.width * ratio)
        new_h = int(img.height * ratio)

        img = img.resize((new_w, new_h), RESAMPLE)

        if current_y + new_h + margin > page_height:
            pages.append(page)
            page = Image.new("RGB", (page_width, page_height), bg_color)
            current_y = margin

        page.paste(img, (margin, current_y))
        current_y += new_h + spacing

    pages.append(page)

    pages[0].save(
        output_pdf,
        save_all=True,
        append_images=pages[1:],
        resolution=150.0,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Extrae tablatura de un video y genera un PDF con capturas."
    )

    parser.add_argument(
        "source",
        help="URL de YouTube o ruta a archivo de video local",
    )

    parser.add_argument(
        "--output",
        default="tablatura.pdf",
        help="Nombre del PDF de salida (default: tablatura.pdf)",
    )

    parser.add_argument(
        "--title",
        default=None,
        help="Título a mostrar en la portada del PDF. Sobrescribe el título de YouTube.",
    )

    parser.add_argument(
        "--channel",
        default=None,
        help="Canal o autor a mostrar en la portada del PDF. Sobrescribe el canal de YouTube.",
    )

    parser.add_argument(
        "--sample-every",
        type=float,
        default=2.0,
        help="Tomar una captura cada X segundos (default: 2.0)",
    )

    parser.add_argument(
        "--crop-top-ratio",
        type=float,
        default=0.46,
        help="Compatibilidad: porcentaje superior a recortar (default: 0.46)",
    )

    parser.add_argument(
        "--crop-y-start",
        type=float,
        default=None,
        help="Inicio vertical del recorte, de 0.0 a 1.0. Útil para tabs abajo.",
    )

    parser.add_argument(
        "--crop-y-end",
        type=float,
        default=None,
        help="Final vertical del recorte, de 0.0 a 1.0. Útil para tabs abajo.",
    )

    parser.add_argument(
        "--hash-threshold",
        type=int,
        default=16,
        help="Compatibilidad: ya no se usa como criterio principal (default: 16)",
    )

    parser.add_argument(
        "--hash-size",
        type=int,
        default=12,
        help="Compatibilidad: ya no se usa como criterio principal (default: 12)",
    )

    parser.add_argument(
        "--diff-threshold",
        type=float,
        default=0.010,
        help="Máxima proporción de píxeles cambiados para considerar duplicado (default: 0.010)",
    )

    parser.add_argument(
        "--compare-window",
        type=int,
        default=1,
        help="Cantidad de capturas recientes contra las que comparar (default: 1)",
    )

    parser.add_argument(
        "--debug-diffs",
        action="store_true",
        help="Guarda imágenes de diff además de las imágenes normalizadas/enmascaradas.",
    )

    parser.add_argument(
        "--start",
        type=float,
        default=0.0,
        help="Segundo inicial desde donde procesar",
    )

    parser.add_argument(
        "--end",
        type=float,
        default=None,
        help="Segundo final hasta donde procesar",
    )

    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Si se pasa, conserva las capturas intermedias en una carpeta",
    )

    parser.add_argument(
        "--keep-comparison-images",
        action="store_true",
        help="Guarda las imágenes usadas para comparar duplicados. Muy útil para debug.",
    )

    parser.add_argument(
        "--save-cleaned",
        action="store_true",
        help="Guardar en el PDF las imágenes con la franja borrada en vez de las originales",
    )

    parser.add_argument(
        "--band-half-width",
        type=int,
        default=90,
        help="Medio ancho de la franja vertical a ignorar en píxeles (default: 90)",
    )

    parser.add_argument(
        "--min-band-pixels",
        type=int,
        default=20,
        help="Mínima concentración de píxeles azul/verde para detectar la franja (default: 20)",
    )

    parser.add_argument(
        "--target-tolerance",
        type=int,
        default=48,
        help="Tolerancia para detectar el color #ccd8f0 (default: 48)",
    )

    args = parser.parse_args()

    if args.start < 0:
        parser.error("--start no puede ser negativo")
    if args.end is not None and args.end <= args.start:
        parser.error("--end debe ser mayor que --start")
    if args.crop_y_start is not None or args.crop_y_end is not None:
        crop_y_start = args.crop_y_start if args.crop_y_start is not None else 0.0
        crop_y_end = args.crop_y_end if args.crop_y_end is not None else 1.0
    else:
        crop_y_start = 0.0
        crop_y_end = args.crop_top_ratio
    try:
        validate_crop_ratios(crop_y_start, crop_y_end)
    except ValueError as exc:
        parser.error(str(exc))

    source = args.source
    output_pdf = Path(args.output).resolve()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        crops_dir = tmp_path / "crops"
        comparison_dir = tmp_path / "comparison" if args.keep_comparison_images else None

        if source.startswith("http://") or source.startswith("https://"):
            print("Descargando video...")
            video_path, metadata, downloaded_start_sec = download_video(
                source,
                tmp_path,
                start_sec=args.start,
                end_sec=args.end,
            )
            metadata = build_video_metadata(
                raw_title=metadata.raw_title,
                channel=metadata.channel,
                source_url=metadata.source_url,
                title_override=args.title,
                channel_override=args.channel,
            )
        else:
            video_path = Path(source).resolve()
            if not video_path.exists():
                raise FileNotFoundError(f"No existe el archivo: {video_path}")
            downloaded_start_sec = 0.0
            metadata = build_video_metadata(
                raw_title=args.title,
                channel=args.channel,
                source_url=None,
                title_override=args.title,
                channel_override=args.channel,
            )

        print(f"Video fuente: {video_path}")
        if metadata.display_title:
            print(f"Título PDF: {metadata.display_title}")
        if metadata.channel:
            print(f"Crédito PDF: {metadata.channel}")

        extract_start_sec = max(0.0, args.start - downloaded_start_sec)
        extract_end_sec = args.end - downloaded_start_sec if args.end is not None else None
        if downloaded_start_sec > 0:
            print(
                f"Fragmento descargado desde {downloaded_start_sec:.2f}s; "
                "procesando desde 0.00s dentro del archivo local."
            )

        options = ExtractionOptions(
            sample_every_sec=args.sample_every,
            crop_y_start_ratio=crop_y_start,
            crop_y_end_ratio=crop_y_end,
            hash_threshold=args.hash_threshold,
            hash_size=args.hash_size,
            diff_threshold=args.diff_threshold,
            compare_window=args.compare_window,
            debug_diffs=args.debug_diffs,
            start_sec=extract_start_sec,
            end_sec=extract_end_sec,
            save_cleaned=args.save_cleaned,
            band_half_width=args.band_half_width,
            min_band_pixels=args.min_band_pixels,
            target_tolerance=args.target_tolerance,
        )

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
            start_sec=options.start_sec,
            end_sec=options.end_sec,
            save_cleaned=options.save_cleaned,
            band_half_width=options.band_half_width,
            min_band_pixels=options.min_band_pixels,
            target_tolerance=options.target_tolerance,
        )

        if stats.captures_kept == 0:
            raise RuntimeError("No se extrajo ninguna captura útil.")

        print("Armando PDF...")
        build_pdf_from_images(crops_dir, output_pdf, metadata=metadata)

        print(f"PDF generado en: {output_pdf}")

        if args.keep_images:
            final_crops = Path.cwd() / "capturas_tablatura"
            if final_crops.exists():
                shutil.rmtree(final_crops)
            shutil.copytree(crops_dir, final_crops)
            print(f"Capturas guardadas en: {final_crops}")

        if args.keep_comparison_images and comparison_dir is not None:
            final_comparison = Path.cwd() / "debug_comparacion_tablatura"
            if final_comparison.exists():
                shutil.rmtree(final_comparison)
            shutil.copytree(comparison_dir, final_comparison)
            print(f"Imágenes de comparación guardadas en: {final_comparison}")


if __name__ == "__main__":
    main()
