# Hosted build of the job applier dashboard. Runs on Hugging Face Spaces (Docker), Render, or any Docker host.
# Microsoft's Playwright image already has Chromium and its system libraries, matching playwright==1.64.0.
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    JOBAGENT_HOME=/data PORT=7860

WORKDIR /app
COPY requirements.txt .
# A single-purpose container, so installing into the image's own Python is fine.
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .
# Hugging Face runs the container as user 1000 (the image's "pwuser" already has that id).
RUN mkdir -p /data && chown -R 1000:1000 /data /app
USER 1000

EXPOSE 7860
HEALTHCHECK CMD python3 -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/healthz')"
CMD ["./deploy/entrypoint.sh"]
