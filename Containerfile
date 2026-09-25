# beherouter — multi-service FastMCP gateway. See docs/DEPLOYMENT.md.
FROM python:3.12-slim

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The image's own Python, never a downloaded one: every venv below is built
# against /usr/local/bin/python3.12.
ENV UV_PYTHON_DOWNLOADS=never

# --- plane-mcp-server, for the in-tree `plane` stdio plugin -------------------
# The plugin's default `cmd` is /opt/plane-mcp/bin/plane-mcp-server; without
# this the published image could not attach its own plugin's defaults. It lives
# in a venv of its OWN, so its FastMCP pin never meets the gateway's.
# --exclude-newer pins the ~80 transitive dependencies, which the top-level
# `==` does not. Build with --build-arg PLANE_MCP_VERSION= to leave it out.
ARG PLANE_MCP_VERSION=0.3.2
ARG PLANE_MCP_EXCLUDE_NEWER=2026-09-24T00:00:00Z
RUN set -eux; \
    if [ -n "${PLANE_MCP_VERSION}" ]; then \
      uv venv /opt/plane-mcp; \
      uv pip install --no-cache --python /opt/plane-mcp/bin/python \
        --exclude-newer "${PLANE_MCP_EXCLUDE_NEWER}" \
        "plane-mcp-server==${PLANE_MCP_VERSION}"; \
    fi

# --- beherouter ----------------------------------------------------------------
WORKDIR /app
COPY . /app

# `--no-sources` ignores the [tool.uv.sources] local-path override for beheaxi
# (there is no ../beheaxi in the build context) and resolves the pinned
# `beheaxi @ git+...@vX.Y.Z` dependency instead. beheaxi is public, so the
# anonymous git fetch needs no token and no BuildKit `--mount=type=secret` —
# a plain RUN builds on any builder. `git` above is what uv uses for git deps.
RUN uv sync --no-sources --no-dev --no-cache

# Non-root by default, so a secure posture does not depend on every deployer
# setting runAsNonRoot. Numeric in USER so Kubernetes can verify runAsNonRoot
# without a passwd lookup. Everything above is root-owned and world-readable:
# the gateway writes nothing, and runs with readOnlyRootFilesystem when /tmp is
# writable (Python skips bytecode it cannot write).
RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin beherouter
USER 1000:1000

ENV PATH="/app/.venv/bin:${PATH}" \
    BEHEROUTER_REGISTRY=/data/registry.toml \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 47100
CMD ["beherouter", "serve", "--host", "0.0.0.0", "--port", "47100"]
