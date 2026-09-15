# gauss-pipeline

Docker image for Vast.ai instances (x86_64 + CUDA) with COLMAP + nerfstudio.

## Base
- `FROM vastai/base-image:cuda-12.8.1-auto` (pinned `--platform=linux/amd64` via build flag)
- Target platform is **linux/amd64 only**. Vast instances are x86_64. Do not build for ARM.

## What is in the image
1. COLMAP dependencies via apt (Boost, Eigen, OpenImageIO, Metis, Glog, Gtest, SQLite, Glew, Qt6, CGAL, Ceres, SuiteSparse, curl, ssl)
2. COLMAP built from source (`https://github.com/colmap/colmap.git`)
3. `pip install nerfstudio`

## Key decisions / history
- Original Dockerfile was broken:
  - `apt-get install` without `update` / `-y`
  - `RUN git clone ... cd colmap` — `cd` does not persist across RUN layers
  - `sudo ninja install` — already root, sudo not installed
  - Fixed with `apt-get update && apt-get install -y --no-install-recommends`, `WORKDIR /tmp`, `cmake -S/-B + cmake --build/--install`.
- BLAS: switched from Intel MKL (`libmkl-full-dev`, `-DBLA_VENDOR=Intel10_64lp`) to OpenBLAS because CMake failed on Linux server with:
  - `Failed to find SuiteSparse - Did not find BLAS library (required for SuiteSparse)`
  - Root cause: `BLA_VENDOR=Intel10_64lp` forces MKL-only lookup, MKL was not discoverable in vastai base image (needs gfortran, brittle on Ubuntu 24.04+, no fallback BLAS installed).
  - Now uses: `gfortran libopenblas-openmp-dev liblapack-dev` + `-DBLA_VENDOR=OpenBLAS`.
  - Per COLMAP docs, on Debian/Ubuntu you MUST use the OpenMP variant (`libopenblas-openmp-dev`, not `libopenblas-dev`/pthread) to avoid N^2 thread explosion with OpenMP. Dockerfile forces it via `update-alternatives --set` for `libblas.so.3` / `liblapack.so.3`.
- MKL vs OpenBLAS: both are BLAS (matrix math for bundle adjustment). MKL = Intel closed-source, fastest on Intel, huge (~1GB), finicky. OpenBLAS = open-source, ~95% speed, tiny, reliable. Chose OpenBLAS for Docker reliability/size.

## Build
On Linux x86_64 server (Vast build box):
```sh
docker build -t gauss-pipeline:latest .
# or explicit platform:
docker build --platform linux/amd64 -t gauss-pipeline:latest .
```

On ARM Mac (cross-build verify only, cannot run CUDA/x86):
```sh
docker buildx create --use # once
docker buildx build --platform linux/amd64 -t gauss-pipeline:latest --load .
# to publish:
docker buildx build --platform linux/amd64 -t <dockerhub-user>/gauss-pipeline:latest --push .
```

## Current state
- Dockerfile builds COLMAP with OpenBLAS-openmp, committed as `dc87e27` ("Switch COLMAP BLAS from MKL to OpenBLAS-openmp").
- Next: verify `docker build` succeeds on Linux server, then `docker push` and use as Vast template.
