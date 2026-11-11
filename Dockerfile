# claude-rotate in a container. See deploy/compose.yaml for how to run it.
#
#   /config  config.json and the token files, mounted read-only
#   /data    state.json, usage files and audit.jsonl, mounted read-write
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLAUDE_ROTATE_CONFIG=/config/config.json \
    CLAUDE_ROTATE_STATE=/data/state.json \
    CLAUDE_ROTATE_AUDIT=/data/audit.jsonl \
    CLAUDE_ROTATE_BIND=0.0.0.0 \
    CLAUDE_ROTATE_ADMIN_MODE=container

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.lock

# --chmod: a checkout made under a strict umask must still be readable by the
# unprivileged user below.
COPY --chmod=0644 rotator.py oai_compat.py LICENSE config.example.json ./
# The admin finds config.example.json at ../config.example.json, so it sits in
# /app and the script in /app/deploy. Create deploy/ first: --chmod would give a
# directory that COPY creates mode 0644, which the unprivileged user cannot enter.
RUN mkdir -m 0755 deploy
COPY --chmod=0644 deploy/claude_rotate_admin.py ./deploy/
# claude-rotate-admin is for a one-off container (see deploy/compose.yaml).
RUN useradd --system --uid 10001 --no-create-home claude-rotate \
 && mkdir /data \
 && chown 10001:10001 /data \
 && printf '#!/bin/sh\nexec python -I /app/deploy/claude_rotate_admin.py "$@"\n' \
      > /usr/local/bin/claude-rotate-admin \
 && chmod 0755 /usr/local/bin/claude-rotate-admin
USER 10001

EXPOSE 8484
# Exec form, so Python is PID 1 and receives SIGHUP (reload) and SIGTERM (stop).
CMD ["python", "rotator.py"]
