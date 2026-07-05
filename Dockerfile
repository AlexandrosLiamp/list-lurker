# Base image ships the exact Playwright build pinned in requirements.txt,
# with Chromium and its system dependencies preinstalled.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Default service: the dashboard + API. The crawler/watcher runs as a second
# container from the same image (see docker-compose.yml).
CMD ["python", "serve.py"]
