FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2, numpy, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.8.3

# Install dependencies (cached layer)
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --without dev

# Copy app code
COPY . .

# Default: run the scheduler
CMD ["python", "-m", "src.scheduler.main"]
