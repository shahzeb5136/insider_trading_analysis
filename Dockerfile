FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Progress output contains box-drawing characters and emoji; without this
    # they raise UnicodeEncodeError when stdout is a pipe rather than a tty.
    PYTHONIOENCODING=utf-8 \
    # Lets `python -m api.build_report` resolve `config`, `fetcher` and `store`.
    PYTHONPATH=/app \
    # matplotlib needs a writable config dir; /root may be read-only.
    MPLCONFIGDIR=/tmp/matplotlib \
    # Defaults matching the Railway volume mount. Override in the dashboard.
    DATA_DIR=/data

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /tmp/matplotlib

EXPOSE 8000

# Shell form on purpose: ${PORT} must be expanded by a shell. Do NOT set a
# startCommand in railway.json — Railway execs that without a shell, so "$PORT"
# would reach uvicorn as a literal string and it would refuse to start.
# The default keeps `docker run` working locally, where PORT is unset.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
