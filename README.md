# gauss-pipeline

Self-hosted **video → 3D Gaussian Splat** pipeline. Runs on rented Vast.ai GPU instances (x86_64 + CUDA) with no third-party cloud, no per-scan fees, and full control over every stage. Capture walkthrough footage on a phone, get a trained splat out.

## How it works

```
phone video → select_frames → ns-process-data (COLMAP) → ns-train (splatfacto) → ns-export → .ply / viewer
```

| Stage | Tool | What it does |
|---|---|---|
| Capture | Blackmagic Camera app (30fps) | Walkthrough footage; slow walk, stop-and-pan at doorways, locked exposure |
| Preprocess | `preprocess/select_frames.py` | Windowed sharpness/exposure competition picks the best frames, drops blur and blown-out frames |
| Reconstruction | COLMAP (built from source) + `ns-process-data` | SIFT features → matching → bundle adjustment → `transforms.json` camera poses |
| Training | `ns-train splatfacto` | Trains the Gaussian splat (~6GB VRAM; `splatfacto-big` needs ~12GB) |
| Export | `ns-export gaussian-splat` | Final `.ply` splat, plus optional `ns-viewer` / `ns-render` |

## Preprocessing (`preprocess/select_frames.py`)

Runs on the instance (upload the small H.264/H.265 video, not frames). Splits the
video into consecutive time windows and keeps the sharpest, well-exposed frames
per window — coverage is guaranteed while blur, darkness and blown-out frames lose.

```sh
python3 preprocess/select_frames.py \
    --input /workspace/data/raw/walkthrough.mp4 \
    --output /workspace/data/raw/frames_curated/ \
    --target-count 400
```

Key flags: `--target-count` (auto-sizes windows; `0` = fixed `--window-sec`),
`--keep-per-window`, `--min-luma/--max-luma/--max-clipped-frac` (exposure veto),
`--no-dedup` / `--dedup-mse` (near-duplicate skipping). Outputs kept
`frame_%05d.jpg` + `keep_report.csv` (per-frame scores and keep/reject reason) +
`summary.json`. CPU-only, dependency-light (`opencv-python-headless` + numpy),
deterministic — runs identically on a laptop or in the image.

## What's in the Docker image

Base: `vastai/base-image:cuda-12.8.1-auto` (**linux/amd64 only** — Vast instances are x86_64; never build for ARM).

1. COLMAP dependencies via apt (Boost, Eigen, OpenImageIO, Metis, Glog, Gtest, SQLite, Glew, Qt6, CGAL, Ceres, SuiteSparse, curl, ssl, gfortran, OpenBLAS-openmp, LAPACK)
2. COLMAP built from source with `-DBLA_VENDOR=OpenBLAS`
3. `pip install nerfstudio`
4. `ffmpeg` + `opencv-python-headless` for the preprocessing stage (`preprocess/select_frames.py`)

## Key decisions

- **Self-hosted over Luma/Polycam/Teleport.** Those are fine for quick scans, but they process in third-party clouds (a problem when scanning people's homes), gate exports and commercial use behind tiers, and give you no levers when reconstruction fails. Here every stage is inspectable and tunable — this pipeline has already survived a real debugging session (see History).
- **OpenBLAS, not Intel MKL.** MKL is marginally faster on Intel but huge (~1GB) and its CMake discovery is brittle. OpenBLAS-openmp is ~95% the speed, tiny, reliable. On Debian/Ubuntu you must use the **OpenMP variant** (`libopenblas-openmp-dev`, forced via `update-alternatives` for libblas/liblapack) — the pthread default causes an N² thread explosion with COLMAP's OpenMP code (per COLMAP docs).
- **Windowed frame selection over naive sampling.** Uniform fps sampling keeps blurry frames; global top-K sharpest clusters on static moments and leaves gaps. Per-window competition guarantees coverage while discarding blur (see `preprocess/` plan).
- **Record3D (iOS) / Polycam pose export as escape hatches.** If COLMAP struggles with a dataset, phone VIO poses can bypass reconstruction entirely via nerfstudio's `record3d` / `polycam` parsers. No Record3D equivalent exists on Android — there, careful capture + COLMAP is the path.

## Capture guide (matters more than any setting)

- Slow walk, steady height; consecutive frames should look nearly identical (70%+ overlap).
- **Doorways: stop, then slow-pan** (~90–180°) from the old room to the new room. These transition frames are the bridges COLMAP needs — without them the reconstruction fragments into disconnected per-room islands.
- Lock exposure/focus; lights on; no motion blur; no moving people in frame.
- 1/120–1/250s shutter; if hallways come out near-black, prefer 1/120 over raising ISO.

## Build

On a Linux x86_64 box:
```sh
docker build -t gauss-pipeline:latest .
```

On ARM Mac (cross-build verify only — CUDA/x86 image can't run locally):
```sh
docker buildx build --platform linux/amd64 -t gauss-pipeline:latest --load .
docker buildx build --platform linux/amd64 -t <dockerhub-user>/gauss-pipeline:latest --push .  # to publish
```

## History (how we got here)

- Original Dockerfile (from `vastai/base-image`) had 3 breaking bugs: `apt-get install` without `update`/`-y`, `RUN ... cd colmap` (cd doesn't persist across layers), `sudo ninja install` (already root, sudo absent). Fixed with `apt-get update && apt-get install -y --no-install-recommends`, `WORKDIR /tmp`, `cmake -S/-B` + `cmake --build/--install` (commit history in git log).
- COLMAP CMake failed with `Failed to find SuiteSparse - Did not find BLAS library`: `-DBLA_VENDOR=Intel10_64lp` forces MKL-only lookup and MKL wasn't discoverable (no gfortran, brittle Ubuntu 24.04+ packaging). Switched to OpenBLAS-openmp (`dc87e27`).
- First real dataset (522 HEIF video frames): ffmpeg couldn't demux the HEIF container, converted via `pillow-heif` to JPG instead.
- `ns-process-data` broke against COLMAP 4.3.0.dev0 twice: `--SiftExtraction.use_gpu` was removed upstream (now `--FeatureExtraction.use_gpu` / `--FeatureMatching.use_gpu`, patched in `colmap_utils.py`), and the downloaded vocab tree was rejected by COLMAP 4.x (fell back to `--matching-method sequential`).
- Reconstruction registered only 0.38% of images — diagnosed as **fragmentation, not failure**: 247/522 frames reconstructed as 9 disconnected per-room components (fast walk + doorway cuts + white walls + blur), and ns-process-data silently exported `sparse/0`, the smallest (2-image) component. Retried matching with sequential overlap 50 to merge components.
- Lesson encoded above: capture technique (doorway pans) decides success before any compute runs.
