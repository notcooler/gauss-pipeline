#!/usr/bin/env python3
"""Video -> curated frames for photogrammetry / Gaussian Splatting.

Decodes every frame of a walkthrough video, scores sharpness (variance of
Laplacian) and exposure (mean luma + clipped-pixel fraction), then runs a
*windowed competition*: the video is split into consecutive time windows and
each window keeps its sharpest, well-exposed frames. Windowing guarantees
temporal coverage (doorway transitions survive) while blur and blown-out
frames are discarded.

Decode path: ffmpeg -> MJPEG image2pipe -> cv2.imdecode. NOTE: do NOT switch
this to `-f rawvideo -pix_fmt bgr24` (or rgb24/yuv420p): for some HEVC .mov
files (observed with Blackmagic 1214x2160 HEVC) the rawvideo pipe yields
frames corrupted with horizontal striping while the encoded-image path
decodes the same frames perfectly. The stripes also fool the sharpness
metric, so the corruption is silent without visual inspection.

Transpose guard: this ffmpeg build's HEVC decoder outputs some files
transposed (W/H swapped) relative to the container dimensions. The script
detects this on the first decoded frame (decoded dims exactly swapped vs
probe, non-square video) and transposes every frame back, logging it loudly.
Without the guard the old code reshaped transposed rawvideo bytes with the
probed dims, which produced the same striping symptom as above.

Codecs: H.264 and H.265 are both accepted (anything ffmpeg decodes works;
anything else prints a warning and is attempted anyway).

Outputs in --output:
  frame_%05d.jpg   kept frames, chronological, JPEG quality --jpg-quality
  keep_report.csv  per-frame scores + keep/reject reason (audit trail)
  summary.json     run parameters + aggregate stats

CPU-only. Deterministic: same input + flags => byte-identical selection.
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ALLOWED_CODECS = {"h264", "hevc"}  # both explicitly supported
SCORE_WIDTH = 720  # scoring resolution (speed); full-res frames are written
PROGRESS_EVERY = 500


def probe(path):
    """Return dict with fps, duration, width, height, codec, total_frames estimate."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,r_frame_rate,duration,nb_frames",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    s = json.loads(out.stdout)["streams"][0]
    num, den = (int(x) for x in s["r_frame_rate"].split("/"))
    fps = num / den if den else 30.0
    duration = float(s.get("duration") or 0.0)
    total = int(s["nb_frames"]) if s.get("nb_frames") not in (None, "N/A") else 0
    if not total and duration:
        total = int(round(duration * fps))
    return {
        "codec": s["codec_name"],
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": fps,
        "duration": duration,
        "total_frames": total,
    }


def iter_jpeg_frames(pipe):
    """Yield raw JPEG byte strings from an ffmpeg image2pipe stdout.

    Splits the byte stream on SOI (FFD8)..EOI (FFD9) markers. Safe because
    ffmpeg's MJPEG output contains no bare FFD8/FFD9 except the frame
    delimiters (0xFF bytes inside entropy data are stuffed as FF00).
    """
    data = b""
    while True:
        chunk = pipe.read(65536)
        if chunk:
            data += chunk
        while True:
            start = data.find(b"\xff\xd8")
            if start < 0:
                data = data[-1:]  # keep possible split-marker prefix
                break
            end = data.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start > 0:
                    data = data[start:]
                break
            yield data[start:end + 2]
            data = data[end + 2:]
        if not chunk:
            break


def decode_frame(jpg_bytes, idx):
    """Decode one MJPEG frame to BGR, exiting loudly on failure."""
    arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        sys.exit(f"error: JPEG decode failed for frame {idx} "
                 f"({len(jpg_bytes)} bytes)")
    return bgr


def score_frame(bgr):
    """Return (sharpness, mean_luma, clipped_frac) for a full-res BGR frame."""
    h, w = bgr.shape[:2]
    scale = SCORE_WIDTH / w
    small = cv2.resize(bgr, (SCORE_WIDTH, int(round(h * scale))),
                       interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean_luma = float(gray.mean())
    clipped = float(((gray < 8) | (gray > 247)).mean())
    return sharpness, mean_luma, clipped, gray


def exposure_ok(mean_luma, clipped, args):
    return (args.min_luma <= mean_luma <= args.max_luma
            and clipped <= args.max_clipped_frac)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, help="input video file")
    ap.add_argument("--output", required=True, help="output dir for frames + reports")
    ap.add_argument("--target-count", type=int, default=400,
                    help="approx frames to keep; window size = duration/target "
                         "(0 = fixed --window-sec instead)")
    ap.add_argument("--window-sec", type=float, default=0.5,
                    help="window length when --target-count 0 or duration unknown")
    ap.add_argument("--keep-per-window", type=int, default=1)
    ap.add_argument("--min-luma", type=float, default=20.0)
    ap.add_argument("--max-luma", type=float, default=235.0)
    ap.add_argument("--max-clipped-frac", type=float, default=0.25,
                    help="max fraction of near-black/white pixels")
    ap.add_argument("--no-dedup", action="store_true",
                    help="disable near-duplicate skipping")
    ap.add_argument("--dedup-mse", type=float, default=8.0,
                    help="skip winner if MSE vs last kept frame is below this")
    ap.add_argument("--jpg-quality", type=int, default=95)
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        sys.exit(f"error: input not found: {src}")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = probe(src)
    if info["codec"] not in ALLOWED_CODECS:
        print(f"warning: codec '{info['codec']}' is not H.264/H.265; "
              f"attempting decode anyway", file=sys.stderr)
    if info["total_frames"] <= 0:
        sys.exit("error: could not determine frame count (no duration/nb_frames)")

    if args.target_count > 0:
        window_frames = max(1, round(info["total_frames"] / args.target_count))
    else:
        window_frames = max(1, round(info["fps"] * args.window_sec))
    print(f"video: {info['width']}x{info['height']} {info['codec']} "
          f"{info['fps']:.2f}fps ~{info['total_frames']} frames",
          file=sys.stderr)
    print(f"window: {window_frames} frames, keep {args.keep_per_window}/window",
          file=sys.stderr)

    # Decode via MJPEG image2pipe (NOT rawvideo bgr24 — see module docstring).
    # -q:v 2 is visually lossless; kept frames are re-encoded at --jpg-quality.
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(src),
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "pipe:1"],
        stdout=subprocess.PIPE,
    )

    report_path = out_dir / "keep_report.csv"
    report = open(report_path, "w", newline="")
    writer = csv.writer(report)
    writer.writerow(["frame_index", "timestamp_sec", "sharpness", "mean_luma",
                     "clipped_frac", "exposure_ok", "kept", "reason"])

    kept_count = 0
    flagged_count = 0
    empty_windows = 0
    transpose = False
    last_kept_gray = None
    window = []  # (index, bgr, sharpness, luma, clipped, gray)
    idx = 0

    def flush_window(win):
        nonlocal kept_count, flagged_count, empty_windows, last_kept_gray
        if not win:
            return
        ranked = sorted(win, key=lambda e: e[2], reverse=True)
        picks = []
        for entry in ranked:
            if len(picks) >= args.keep_per_window:
                break
            _, _, _, luma, clipped, _ = entry
            if exposure_ok(luma, clipped, args):
                picks.append((entry, True))
        for entry in ranked:  # soften veto: keep sharpest anyway, flagged
            if len(picks) >= args.keep_per_window:
                break
            if entry not in (p[0] for p in picks):
                picks.append((entry, False))
        if not picks:
            empty_windows += 1
        for entry, ok in picks:
            i, bgr, sharp, luma, clipped, gray = entry
            if (not args.no_dedup and last_kept_gray is not None
                    and last_kept_gray.shape == gray.shape
                    and float(((gray.astype(np.float32) - last_kept_gray) ** 2).mean())
                    < args.dedup_mse):
                writer.writerow([i, f"{i / info['fps']:.3f}", f"{sharp:.1f}",
                                 f"{luma:.1f}", f"{clipped:.4f}", ok, False,
                                 "dedup_skip"])
                continue
            name = f"frame_{kept_count:05d}.jpg"
            ok_write, buf = cv2.imencode(
                ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, args.jpg_quality])
            if not ok_write:
                sys.exit(f"error: JPEG encode failed for frame {i}")
            (out_dir / name).write_bytes(buf.tobytes())
            reason = "kept" if ok else "kept_exposure_flagged"
            if not ok:
                flagged_count += 1
            writer.writerow([i, f"{i / info['fps']:.3f}", f"{sharp:.1f}",
                             f"{luma:.1f}", f"{clipped:.4f}", ok, True, reason])
            kept_count += 1
            last_kept_gray = gray.astype(np.float32)
        picked_ids = {id(p[0]) for p in picks}
        for entry in win:
            if id(entry) not in picked_ids:
                i, _, sharp, luma, clipped, _ = entry
                writer.writerow([i, f"{i / info['fps']:.3f}", f"{sharp:.1f}",
                                 f"{luma:.1f}", f"{clipped:.4f}",
                                 exposure_ok(luma, clipped, args), False,
                                 "not_sharpest_in_window"])

    for jpg in iter_jpeg_frames(ffmpeg.stdout):
        bgr = decode_frame(jpg, idx)
        if idx == 0:
            dh, dw = bgr.shape[:2]
            pw, ph = info["width"], info["height"]
            if dh == ph and dw == pw:
                transpose = False
            elif dh == pw and dw == ph and pw != ph:
                transpose = True
                print(f"note: decoder outputs {dw}x{dh} (transposed vs "
                      f"container {pw}x{ph}); transposing frames back",
                      file=sys.stderr)
            else:
                sys.exit(f"error: decoded frame is {dw}x{dh} but container "
                         f"is {pw}x{ph}; refusing to guess orientation")
        if transpose:
            bgr = cv2.transpose(bgr)
        sharp, luma, clipped, gray = score_frame(bgr)
        window.append((idx, bgr.copy(), sharp, luma, clipped, gray))
        if len(window) >= window_frames:
            flush_window(window)
            window = []
        idx += 1
        if idx % PROGRESS_EVERY == 0:
            print(f"  scored {idx}/{info['total_frames']}...", file=sys.stderr)
    flush_window(window)
    rc = ffmpeg.wait()
    if rc != 0:
        sys.exit(f"error: ffmpeg decode exited with code {rc} "
                 f"after {idx} frames")
    report.close()

    summary = {
        "input": str(src),
        "codec": info["codec"],
        "fps": round(info["fps"], 3),
        "duration_sec": round(info["duration"], 3),
        "total_frames": idx,
        "params": {k: vars(args)[k] for k in (
            "target_count", "window_sec", "keep_per_window", "min_luma",
            "max_luma", "max_clipped_frac", "no_dedup", "dedup_mse",
            "jpg_quality")},
        "window_frames": window_frames,
        "transposed": transpose,
        "kept_count": kept_count,
        "exposure_flagged": flagged_count,
        "empty_windows": empty_windows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"done: kept {kept_count}/{idx} frames "
          f"({flagged_count} exposure-flagged, {empty_windows} empty windows)",
          file=sys.stderr)
    print(f"report: {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
