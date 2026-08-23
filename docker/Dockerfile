# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
ARG REGISTRY_MIRROR
FROM ${REGISTRY_MIRROR:-docker.io}/python:${PYTHON_VERSION}-alpine AS base

ARG PYPI_MIRROR
ARG LOCAL_WHEEL
ARG PACKAGE_VERSION

# Prevents Python from writing pyc files and enables unbuffered output.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-privileged user and group (alpine syntax).
RUN addgroup -g 1000 appgroup && \
    adduser -D -u 1000 -G appgroup appuser

# Install su-exec for runtime user switching in entrypoint.
RUN apk add --no-cache su-exec

# Copy only the dist/ directory for local wheels (never copy the full tree —
# even with .dockerignore, a broad COPY risks leaking secrets into layers).
COPY mansio/dist/ /tmp/dist/

# Install mansio: local wheel > specific version > latest from PyPI
#
# PIP_ARGS is built once to avoid repeating the PYPI_MIRROR conditional.
# After install, pip and setuptools are removed — this is a locked-down
# production image; no further pip installs are expected inside the container.
RUN PIP_ARGS="--no-cache-dir"; \
    [ -n "$PYPI_MIRROR" ] && PIP_ARGS="$PIP_ARGS -i $PYPI_MIRROR"; \
    if [ -n "$LOCAL_WHEEL" ] && [ -f "/tmp/dist/$LOCAL_WHEEL" ]; then \
        echo "Installing from local wheel: $LOCAL_WHEEL"; \
        pip install $PIP_ARGS "/tmp/dist/${LOCAL_WHEEL}"; \
    elif [ -n "$PACKAGE_VERSION" ]; then \
        echo "Installing mansio version: $PACKAGE_VERSION"; \
        pip install $PIP_ARGS "mansio==$PACKAGE_VERSION"; \
    else \
        echo "Installing latest mansio from PyPI"; \
        pip install $PIP_ARGS "mansio"; \
    fi && \
    pip uninstall -y pip setuptools 2>/dev/null || true && \
    rm -rf /tmp/dist/

# Data directory — mount a volume here for persistent SQLite storage.
RUN mkdir -p /data && chown appuser:appgroup /data
VOLUME /data

# Entrypoint fixes /data ownership and drops to appuser via su-exec.
# Set PUID/PGID environment variables to match your host user (default: 1000).
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8741 8742

ENTRYPOINT ["/entrypoint.sh"]
CMD ["mansio", "serve", "--db", "/data/mansio.db", "--http", "0.0.0.0:8742", "--remote", "--admin-port", "8741"]
