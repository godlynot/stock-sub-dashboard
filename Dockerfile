# Render Dockerfile — explicit build for the web service
# Render will use this if present, otherwise it auto-detects Python via requirements.txt

FROM python:3.11-slim

WORKDIR /opt/render/project/src

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Persistent disk is mounted at /opt/render/project/src/data on Render
# Make sure the data directory exists with write permissions
RUN mkdir -p data && chmod 777 data

# Default port for Render web services
ENV PORT=10000
EXPOSE 10000

# Use gunicorn for production; threads because fetcher is I/O bound
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:10000", "dashboard:app"]