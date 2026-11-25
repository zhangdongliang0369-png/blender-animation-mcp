FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends blender \
    libegl1 libegl-mesa0 libgl1-mesa-dri && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py animate.py ./
RUN useradd --system blenderapi
USER blenderapi
ENV LIBGL_ALWAYS_SOFTWARE=1 BLENDER_RENDER_DIR=/tmp/blender-renders \
    LP_NUM_THREADS=1 OMP_NUM_THREADS=1 MALLOC_ARENA_MAX=2
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000} --no-access-log"]
