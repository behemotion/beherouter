# beherouter — multi-service FastMCP gateway. See docs/DEPLOYMENT.md.
FROM python:3.12-slim

# Non-root by default, so a secure posture does not depend on every deployer
# setting runAsNonRoot; the user is created here and switched to at the end.
# apt packages are deliberately unpinned (hadolint DL3008): Debian's archive
# drops superseded versions, so an `=version` pin breaks the build at the next
# security update -- the python:3.12-slim tag is what moves them.
# hadolint ignore=DL3008
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin beherouter

# Pinned by version AND digest (the multi-arch index), never `latest`: two
# builds of one git tag must use the same uv, or the published image is not
# reproducible from its own revision. Bump both together; the digest is
# `docker-content-digest` of ghcr.io/astral-sh/uv:<version>.
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
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

# `--frozen` installs exactly what the committed uv.lock records and never
# re-resolves -- the same assertion CI makes, so a pyproject/uv.lock divergence
# fails the image build instead of silently shipping a different resolution.
# There is no `--no-sources`: pyproject.toml has no [tool.uv.sources] table (a
# local-path override for beheaxi was dropped so a public clone installs), uv
# refuses the two flags together, and the lockfile -- which CI syncs --frozen
# in a checkout with no sibling ../beheaxi -- is what guards against one
# coming back. beheaxi is the pinned `beheaxi @ git+...@vX.Y.Z` dependency; it
# is public, so the anonymous git fetch needs no token and no BuildKit secret
# mount. `git` above is what uv uses for git deps.
RUN uv sync --frozen --no-dev --no-cache

# Numeric in USER so Kubernetes can verify runAsNonRoot without a passwd
# lookup (the user itself is created in the first RUN). Everything above is
# root-owned and world-readable: the gateway writes nothing, and runs with
# readOnlyRootFilesystem when /tmp is writable (Python skips bytecode it cannot
# write).
USER 1000:1000

ENV PATH="/app/.venv/bin:${PATH}" \
    BEHEROUTER_REGISTRY=/data/registry.toml \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 47100
CMD ["beherouter", "serve", "--host", "0.0.0.0", "--port", "47100"]
