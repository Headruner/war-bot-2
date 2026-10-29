# Use a standard python slim image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/usr/local/bin:${PATH}"

# Set working directory
WORKDIR /app

# Install system dependencies, including the system-level python-yaml
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    libjpeg-dev \
    libtiff6 \
    python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create a non-root user for security. WORKDIR created /app as root and
# COPY --chown does not change the existing directory, so hand /app and the
# database directory to botuser explicitly (a fresh named volume inherits it).
RUN useradd -m botuser && \
    mkdir -p /app/data && \
    chown botuser:botuser /app /app/data

# Copy project files and set ownership to the non-root user
COPY --chown=botuser:botuser . .

# Switch to the non-root user
USER botuser

# Healthcheck
HEALTHCHECK --interval=60s --timeout=30s --retries=3 \
  CMD [python, "-c", "import json; print(json.dumps({'status': 'ok'}))"]

# Start the app
CMD ["python", "-m", "app.main"]
