FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
COPY bridge.py hyemi_*.py naver_*.py /app/
# The shared ingress validator is vendored; no host Python modules are required.
# The ingress secret and MCP config are mounted read-only at runtime, never built into this image.
CMD ["python", "/app/bridge.py"]
