# SkyGuitar PDF Extractor

A small Python tool for extracting guitar tab screenshots from SkyGuitar YouTube videos and turning them into a clean PDF.

Suggested README screenshot: add a before/after image at the top showing a SkyGuitar video frame with the moving blue playhead next to the generated PDF page. That immediately explains the problem this project solves.

## How It Works

The script downloads or opens a video, samples frames every few seconds, crops the tab area at the top, ignores the moving blue playhead when comparing frames, removes duplicates, and builds a PDF from the unique tab screenshots.

## Setup

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install pillow opencv-python numpy yt-dlp
```

## Usage

From a YouTube URL:

```bash
.venv/bin/python tab_extractor.py "https://youtu.be/VIDEO_ID" --output tablatura.pdf
```

From a local video file:

```bash
.venv/bin/python tab_extractor.py video.mp4 --output tablatura.pdf
```

Useful options:

```bash
--start 195                  # start processing at a specific second
--end 300                    # stop processing at a specific second
--keep-images                # keep extracted tab screenshots
--keep-comparison-images     # keep debug comparison images
--debug-diffs                # save visual diff images for duplicate decisions
```

The current defaults are tuned for SkyGuitar-style videos:

```bash
--sample-every 2
--crop-top-ratio 0.46
--band-half-width 90
--diff-threshold 0.010
```

## Example

```bash
.venv/bin/python tab_extractor.py "https://youtu.be/VnDcVqRxSmA?si=jjBvE64DAhj_mvO2" \
  --start 195 \
  --output tablatura.pdf
```
