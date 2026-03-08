### Stage 1: Build React frontend
FROM node:18-slim AS frontend-build
WORKDIR /frontend
COPY src/dashboard/frontend/package.json src/dashboard/frontend/package-lock.json* ./
RUN npm install
COPY src/dashboard/frontend/ ./
RUN npm run build

### Stage 2: Python app
FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2, numpy, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.8.3

# Install CPU-only PyTorch first (no CUDA — saves ~2 GB RAM + disk)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install dependencies (cached layer)
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --without dev

# Copy app code
COPY . .

# Copy React build from stage 1
COPY --from=frontend-build /frontend/build /app/src/dashboard/frontend/build

# Prevent PyTorch/XGBoost libomp segfault
ENV OMP_NUM_THREADS=1

# Default: run the scheduler
CMD ["python", "-m", "src.scheduler"]
