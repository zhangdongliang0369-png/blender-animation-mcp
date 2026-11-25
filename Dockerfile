FROM python:3.12-slim-trixie AS blender-v2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /download
RUN curl -fsSLo blender.tar.xz --retry 3 https://download.blender.org/release/Blender5.3/blender-5.3.0-linux-x64.tar.xz \
    && echo "dacb8264197a3f7fda6001285a5a506fc97ee943881261c2c3bd63c882f22e05  blender.tar.xz" | sha256sum -c - \
    && mkdir /blender \
    && tar -xJf blender.tar.xz --strip-components=1 -C /blender \
    && rm blender.tar.xz

FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends blender=4.3.2+dfsg-2 \
    libegl1 libegl-mesa0 libgl1-mesa-dri bubblewrap ffmpeg util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py animate.py docker-entrypoint.sh ./
COPY v2 ./v2
COPY --from=blender-v2 /blender /opt/blender-5.3.0
RUN useradd --system blenderapi
ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]
ENV LIBGL_ALWAYS_SOFTWARE=1 BLENDER_RENDER_DIR=/tmp/blender-renders \
    LP_NUM_THREADS=1 OMP_NUM_THREADS=1 MALLOC_ARENA_MAX=2 \
    BLENDER_V2_BINARY=/opt/blender-5.3.0/blender
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000} --no-access-log"]
