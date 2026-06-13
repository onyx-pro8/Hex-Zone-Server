# Zone Weaver Backend - Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port. DigitalOcean App Platform (and most PaaS) inject a PORT env var
# and probe that port; their default is 8080. Default to 8080 here so the
# readiness probe reaches us even when PORT is not injected; an injected PORT
# still wins at runtime.
ENV PORT=8080
EXPOSE 8080

# Health check honours the runtime PORT so it matches the bound port.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=10).read()"

# Run application. Bind to the platform-provided $PORT (falls back to 8080).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
