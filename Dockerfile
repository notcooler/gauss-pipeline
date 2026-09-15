FROM vastai/base-image:cuda-12.8.1-auto

# Colmap Deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    cmake \
    ninja-build \
    build-essential \
    libboost-program-options-dev \
    libboost-graph-dev \
    libboost-system-dev \
    libeigen3-dev \
    libopenimageio-dev \
    openimageio-tools \
    libmetis-dev \
    libgoogle-glog-dev \
    libgtest-dev \
    libgmock-dev \
    libsqlite3-dev \
    libglew-dev \
    qt6-base-dev \
    libqt6opengl6-dev \
    libqt6openglwidgets6 \
    qt6-svg-dev \
    libcgal-dev \
    libceres-dev \
    libsuitesparse-dev \
    libcurl4-openssl-dev \
    libssl-dev \
    libmkl-full-dev \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/include/opencv4

# Colmap build
WORKDIR /tmp
RUN git clone https://github.com/colmap/colmap.git && \
    cmake -S colmap -B colmap/build -GNinja -DBLA_VENDOR=Intel10_64lp && \
    cmake --build colmap/build && \
    cmake --install colmap/build && \
    rm -rf colmap

# nerfstudio
RUN pip install --no-cache-dir nerfstudio
