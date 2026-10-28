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

# Copy project files
COPY . .

# Create a non-root user for security
RUN useradd -m botuser
USER botuser

# Healthcheck
HEALTHCHECK --interval=60s --timeout=30s --retries=3 \
  CMD [python, "-c", "import json; print(json.dumps({'status': 'ok'}))"]

# Start the app
CMD ["python", "-m", "app.main"]
