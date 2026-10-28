# Build environment
FROM python:3.11-slim as builder
WORKDIR /app
RUN apt-get update && apt-get install -y gcc libjpeg-dev libtiff6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir PyYAML

# Runtime environment
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
ENV PATH="/usr/local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# Install runtime dependencies for Pillow
RUN apt-get update && apt-get install -y libjpeg62-turbo libtiff6 && rm -rf /var/lib/apt/lists/*

COPY . .

# Create a non-root user for security
RUN useradd -m botuser
USER botuser

# Healthcheck
HEALTHCHECK --interval=60s --timeout=30s --retries=3 \
  CMD [python, "-c", "import json; print(json.dumps({'status': 'ok'}))"]

# Start the app
CMD ["python", "-m", "app.main"]
