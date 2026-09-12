# beherouter — multi-service FastMCP gateway. See docs/DEPLOYMENT.md.
FROM python:3.12-slim

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git; \
    rm -rf /var/lib/apt/lists/*

# --- beherouter ----------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY . /app

# `--no-sources` ignores the [tool.uv.sources] local-path override for beheaxi
# (there is no ../beheaxi in the build context) and resolves the pinned
# `beheaxi @ git+...@vX.Y.Z` dependency instead. beheaxi is public, so the
# anonymous git fetch needs no token and no BuildKit `--mount=type=secret` —
# a plain RUN builds on any builder. `git` above is what uv uses for git deps.
RUN uv sync --no-sources --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    BEHEROUTER_REGISTRY=/data/registry.toml

EXPOSE 47100
CMD ["beherouter", "serve", "--host", "0.0.0.0", "--port", "47100"]
