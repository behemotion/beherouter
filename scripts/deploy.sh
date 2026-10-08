#!/usr/bin/env bash
# deploy.sh -- deploy beherouter to a single podman host, verify it, and roll
# back automatically if it does not come up healthy.
#
# The order is the whole point (docs/DEPLOYMENT.md § Upgrading):
#
#   1. get the new image        pull the published release, or --build it here
#   2. pre-deploy gate          `registry-lint` INSIDE THE NEW IMAGE, with the
#                               env it will serve with. A failure stops here,
#                               with the running gateway untouched.
#   3. record + switch          the registry is SNAPSHOTTED per deploy (so a
#                               rollback restores the old config too, not just
#                               the old image); the running container is
#                               stopped and kept (renamed <name>-previous),
#                               never deleted first
#   4. verify                   /healthz (`ok`; `degraded` fails unless
#                               --allow-degraded), then optionally
#                               `health --deep --json` inside the container --
#                               a real credentialed call per surface
#   5. on any failure after 3   remove the new container, restore and restart
#                               the previous one, verify it, exit non-zero
#
# A green /healthz proves the process is up and what attached at startup; it
# cannot see a revoked backend credential. Use --deep for that.
#
# Requires: bash, podman (rootless or rootful), curl. Nothing else.
set -euo pipefail

PROG="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_REPOSITORY="ghcr.io/behemotion/beherouter"
LOCAL_REPOSITORY="localhost/beherouter"
APP_PORT=47100

usage() {
  cat <<EOF
Usage: $PROG [options]

Deploy beherouter on this host with podman: lint the registry inside the new
image, replace the running container, verify, and roll back on failure.

Image (pick one; default: ${DEFAULT_REPOSITORY}:<pyproject version>):
  --tag TAG             image tag, e.g. 0.2.5 (a leading 'v' is stripped)
  --image REF           full image reference; overrides --tag
  --build               build ${LOCAL_REPOSITORY}:<tag> from this checkout
                        (Containerfile at the repo root) instead of pulling
  --no-pull             use a local image as-is; do not pull

Configuration:
  --registry PATH       host registry.toml (default: ./data/registry.toml).
                        Each deploy mounts a read-only SNAPSHOT of it, so a
                        rollback also restores the previous registry; edit
                        this file and re-run to change the registry
  --state-dir DIR       where snapshots live (default: <registry dir>/.deploy)
  --env-file PATH       env file passed to the container: BEHEROUTER_GATEWAY_TOKEN,
                        backend credentials, auth settings (default: ./.env)
  --name NAME           container name (default: beherouter)
  --publish ADDR:PORT   host address:port for the gateway's port ${APP_PORT}
                        (default: 127.0.0.1:${APP_PORT}; keep it loopback --
                        the reverse proxy is the network surface)
  --network NET         also join a podman network (e.g. a shared one that
                        same-host backends are reachable on by name)

Verification:
  --deep                also run 'beherouter health --deep --json' in the
                        new container; any failing surface fails the deploy
  --allow-degraded      accept /healthz status "degraded" (a surface failed
                        to attach and is being retried); default: fail
  --timeout SECONDS     how long to wait for /healthz (default: 90)
  --keep-previous       keep the stopped <name>-previous container after a
                        successful deploy (default: remove it)

Other:
  --dry-run             print what would run; change nothing
  -h, --help            this text

Exit status: 0 deployed and verified; 1 failed (rolled back where a previous
container existed); 2 usage error.

Examples:
  $PROG --tag 0.2.5 --deep
  $PROG --build --registry /srv/beherouter/registry.toml --env-file /srv/beherouter/.env
EOF
}

die() { echo "$PROG: error: $*" >&2; exit 1; }
usage_error() { echo "$PROG: $*" >&2; echo "Try '$PROG --help'." >&2; exit 2; }
log() { echo "==> $*"; }
warn() { echo "!!! $*" >&2; }

# ---- arguments ---------------------------------------------------------------
TAG=""
IMAGE=""
BUILD=0
PULL=1
REGISTRY="./data/registry.toml"
STATE_DIR=""
ENV_FILE="./.env"
NAME="beherouter"
PUBLISH="127.0.0.1:${APP_PORT}"
NETWORK=""
DEEP=0
ALLOW_DEGRADED=0
TIMEOUT=90
KEEP_PREVIOUS=0
DRY_RUN=0

need_arg() { [[ $# -ge 2 && -n "$2" ]] || usage_error "$1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)            need_arg "$@"; TAG="${2#v}"; shift 2 ;;
    --image)          need_arg "$@"; IMAGE="$2"; shift 2 ;;
    --build)          BUILD=1; shift ;;
    --no-pull)        PULL=0; shift ;;
    --registry)       need_arg "$@"; REGISTRY="$2"; shift 2 ;;
    --state-dir)      need_arg "$@"; STATE_DIR="$2"; shift 2 ;;
    --env-file)       need_arg "$@"; ENV_FILE="$2"; shift 2 ;;
    --name)           need_arg "$@"; NAME="$2"; shift 2 ;;
    --publish)        need_arg "$@"; PUBLISH="$2"; shift 2 ;;
    --network)        need_arg "$@"; NETWORK="$2"; shift 2 ;;
    --deep)           DEEP=1; shift ;;
    --allow-degraded) ALLOW_DEGRADED=1; shift ;;
    --timeout)        need_arg "$@"; TIMEOUT="$2"; shift 2 ;;
    --keep-previous)  KEEP_PREVIOUS=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage_error "unknown argument: $1" ;;
  esac
done

[[ "$TIMEOUT" =~ ^[0-9]+$ && "$TIMEOUT" -gt 0 ]] || usage_error "--timeout must be a positive integer"
[[ "$PUBLISH" =~ ^(.+):([0-9]+)$ ]] || usage_error "--publish must be ADDR:PORT, e.g. 127.0.0.1:${APP_PORT}"
HOST_ADDR="${BASH_REMATCH[1]}"
HOST_PORT="${BASH_REMATCH[2]}"
# The address /healthz is polled on: a wildcard bind is reachable on loopback.
case "$HOST_ADDR" in
  0.0.0.0|'[::]'|::) PROBE_ADDR="127.0.0.1" ;;
  *)                 PROBE_ADDR="$HOST_ADDR" ;;
esac
HEALTH_URL="http://${PROBE_ADDR}:${HOST_PORT}/healthz"
PREVIOUS="${NAME}-previous"

if [[ $BUILD -eq 1 && -n "$IMAGE" ]]; then
  usage_error "--build and --image are mutually exclusive"
fi

if [[ -z "$IMAGE" ]]; then
  if [[ -z "$TAG" ]]; then
    [[ -f "$REPO_ROOT/pyproject.toml" ]] \
      || usage_error "no --tag/--image and no pyproject.toml at $REPO_ROOT to read a version from"
    TAG="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO_ROOT/pyproject.toml" | head -n 1)"
    [[ -n "$TAG" ]] || die "could not read a version from $REPO_ROOT/pyproject.toml"
  fi
  if [[ $BUILD -eq 1 ]]; then
    IMAGE="${LOCAL_REPOSITORY}:${TAG}"
  else
    IMAGE="${DEFAULT_REPOSITORY}:${TAG}"
  fi
fi

[[ -f "$REGISTRY" ]] || die "registry not found: $REGISTRY (pass --registry)"
[[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE (pass --env-file; it must set BEHEROUTER_GATEWAY_TOKEN unless BEHEROUTER_AUTH_MODE=oidc)"
REGISTRY="$(cd "$(dirname "$REGISTRY")" && pwd)/$(basename "$REGISTRY")"
STATE_DIR="${STATE_DIR:-$(dirname "$REGISTRY")/.deploy}"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

command -v podman >/dev/null || die "podman not found"
command -v curl >/dev/null || die "curl not found"
PODMAN_OK=1
if ! podman info >/dev/null 2>&1; then
  # A dry run still prints the plan; it just cannot see a running container.
  [[ $DRY_RUN -eq 1 ]] || die "podman is installed but not reachable ('podman info' fails)"
  PODMAN_OK=0
  warn "podman is not reachable; the dry run assumes no container is running"
fi

# ---- helpers -----------------------------------------------------------------
# Every state-changing command goes through run(), so --dry-run is exact.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

# The host path a container mounts at /data/registry.toml.
registry_source() {
  podman container inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/data/registry.toml"}}{{.Source}}{{end}}{{end}}' \
    "$1" 2>/dev/null
}

# Delete a snapshot this script made (only ever inside STATE_DIR).
drop_snapshot() {
  local f="$1"
  [[ -n "$f" && "$f" == "$STATE_DIR"/registry.*.toml ]] && rm -f "$f"
  return 0
}

container_exists() { [[ $PODMAN_OK -eq 1 ]] && podman container exists "$1" 2>/dev/null; }

# Labels the container THIS run creates, so a rollback removes only that one --
# never the previous container, whatever step the failure interrupted. Also
# names this deploy's registry snapshot.
DEPLOY_ID="$(date +%Y%m%dT%H%M%S)-$$"
# The env file needs no snapshot: podman reads it once, at container creation,
# so each container already carries its own copy. The registry is re-read from
# its mount at every boot -- a rollback against a shared, since-edited file
# would restart the old image on the NEW (failing) config.
SNAPSHOT="${STATE_DIR}/registry.${DEPLOY_ID}.toml"

# The run arguments shared by the lint pre-flight and the gateway itself, so
# the lint sees exactly the environment and mounts the gateway will.
COMMON_ARGS=(--env-file "$ENV_FILE")
# :z (shared label), never :Z: a private relabel would lock other containers
# out of the file on an SELinux host. Read-only: the serving gateway writes
# nothing.
COMMON_ARGS+=(-v "${SNAPSHOT}:/data/registry.toml:ro,z")
# The image runs as UID 1000. Under rootless podman, map the invoking user onto
# it so a 0600 registry or env-referenced file you own stays readable -- the
# same mapping podman-compose.yml uses.
if [[ $PODMAN_OK -eq 1 && "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" == "true" ]]; then
  COMMON_ARGS+=(--userns "keep-id:uid=1000,gid=1000")
fi
if [[ -n "$NETWORK" ]]; then
  COMMON_ARGS+=(--network "$NETWORK")
fi

# Poll /healthz until it answers. Prints the body; returns 0 on "ok", 0 on
# "degraded" only with --allow-degraded, 1 otherwise.
wait_healthy() {
  local container="$1" body="" deadline=$((SECONDS + TIMEOUT))
  while (( SECONDS < deadline )); do
    if [[ "$(podman container inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]]; then
      warn "container $container is not running (an attach or config error at startup?)"
      return 1
    fi
    if body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
      echo "    $body"
      if [[ "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"ok\" ]]; then
        return 0
      elif [[ "$body" =~ \"status\"[[:space:]]*:[[:space:]]*\"degraded\" ]]; then
        if [[ $ALLOW_DEGRADED -eq 1 ]]; then
          warn "/healthz is degraded (accepted: --allow-degraded)"
          return 0
        fi
        warn "/healthz is degraded: a surface failed to attach (see 'failed'); pass --allow-degraded to accept"
        return 1
      fi
      warn "/healthz answered without a recognised status"
      return 1
    fi
    sleep 2
  done
  warn "/healthz did not answer at $HEALTH_URL within ${TIMEOUT}s"
  return 1
}

deep_health() {
  local container="$1" out rc=0
  out="$(podman exec "$container" beherouter health --deep --json 2>&1)" || rc=$?
  printf '    %s\n' "${out//$'\n'/$'\n'    }"
  if [[ $rc -ne 0 ]]; then
    warn "health --deep failed (exit $rc): a surface did not attach, its probe failed, or a pinned tool is gone"
    return 1
  fi
}

# ---- rollback ----------------------------------------------------------------
SWITCHED=0      # 1 once the running container may have been touched
HAD_PREVIOUS=0
PREV_IMAGE_NAME="(none)"
PREV_RUNNING=false
DONE=0

is_ours() {
  [[ "$(podman container inspect --format '{{ index .Config.Labels "io.beherouter.deploy-id" }}' "$1" 2>/dev/null)" == "$DEPLOY_ID" ]]
}

# Runs from the EXIT trap: every step tolerates failure, so one stuck command
# cannot stop the previous gateway from being restored.
rollback() {
  warn "deploy of $IMAGE failed; rolling back"
  if container_exists "$NAME" && is_ours "$NAME"; then
    warn "last log lines of the failed container:"
    podman logs --tail 50 "$NAME" >&2 2>&1 || true
    podman rm -f "$NAME" >/dev/null 2>&1 || warn "could not remove the failed container $NAME"
  fi
  drop_snapshot "$SNAPSHOT"
  if [[ $HAD_PREVIOUS -eq 0 ]]; then
    warn "no previous container to restore; nothing is serving on $PUBLISH"
    return
  fi
  if ! container_exists "$NAME" && container_exists "$PREVIOUS"; then
    podman rename "$PREVIOUS" "$NAME" || { warn "could not rename $PREVIOUS back to $NAME"; return; }
  fi
  if [[ "$PREV_RUNNING" != "true" ]]; then
    warn "restored $NAME ($PREV_IMAGE_NAME), left stopped as it was found"
    return
  fi
  podman start "$NAME" >/dev/null || { warn "could not start $NAME; check 'podman logs $NAME'"; return; }
  if wait_healthy "$NAME"; then
    warn "rolled back: $NAME is running $PREV_IMAGE_NAME again"
  else
    warn "rolled back to $PREV_IMAGE_NAME, but it is not healthy either: check 'podman logs $NAME'"
    warn "(it runs on its own registry snapshot: $(registry_source "$NAME"))"
  fi
}

on_exit() {
  local rc=$?
  if [[ $SWITCHED -eq 1 && $DONE -eq 0 && $DRY_RUN -eq 0 ]]; then
    trap - EXIT INT TERM
    rollback
    exit 1
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

# ---- 1. image ----------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then log "DRY RUN -- nothing below is executed"; fi
log "1/5 image: $IMAGE"
if [[ $BUILD -eq 1 ]]; then
  [[ -f "$REPO_ROOT/Containerfile" ]] || die "--build needs a checkout: no Containerfile at $REPO_ROOT"
  run podman build -t "$IMAGE" -f "$REPO_ROOT/Containerfile" "$REPO_ROOT"
elif [[ $PULL -eq 1 ]]; then
  run podman pull "$IMAGE"
fi

# ---- 2. pre-deploy gate --------------------------------------------------------
log "2/5 pre-deploy gate: registry-lint inside $IMAGE"
echo "    snapshot $REGISTRY -> $SNAPSHOT"
run mkdir -p "$STATE_DIR"
run cp -p "$REGISTRY" "$SNAPSHOT"
if [[ $DRY_RUN -eq 1 ]]; then
  run podman run --rm "${COMMON_ARGS[@]}" "$IMAGE" \
    beherouter registry-lint --path /data/registry.toml --json
elif ! podman run --rm "${COMMON_ARGS[@]}" "$IMAGE" \
       beherouter registry-lint --path /data/registry.toml --json; then
  drop_snapshot "$SNAPSHOT"
  die "registry-lint refused $REGISTRY in $IMAGE; nothing was changed"
fi

# ---- 3. record + switch --------------------------------------------------------
PREV_IMAGE_NAME="(none)"
PREV_IMAGE_ID=""
PREV_RUNNING=false
if ! container_exists "$NAME" && container_exists "$PREVIOUS"; then
  # Only an interrupted deploy or rollback leaves this shape, and $PREVIOUS may
  # be the last known-good gateway: never delete it automatically.
  die "found $PREVIOUS but no $NAME -- an earlier deploy was interrupted. Restore it with 'podman rename $PREVIOUS $NAME && podman start $NAME', or remove it, then re-run"
fi
if container_exists "$NAME"; then
  HAD_PREVIOUS=1
  PREV_IMAGE_NAME="$(podman container inspect --format '{{.ImageName}}' "$NAME")"
  PREV_IMAGE_ID="$(podman container inspect --format '{{.Image}}' "$NAME")"
  PREV_RUNNING="$(podman container inspect --format '{{.State.Running}}' "$NAME")"
  log "3/5 switch: recording $NAME = $PREV_IMAGE_NAME (${PREV_IMAGE_ID:0:12}, running=$PREV_RUNNING)"
else
  log "3/5 switch: no container named $NAME yet (first deploy; no rollback target)"
fi

if container_exists "$PREVIOUS"; then
  log "    removing a stale $PREVIOUS from an earlier deploy"
  stale_snapshot="$(registry_source "$PREVIOUS" || true)"
  run podman rm -f "$PREVIOUS"
  if [[ $DRY_RUN -eq 0 ]]; then drop_snapshot "$stale_snapshot"; fi
fi

if [[ $HAD_PREVIOUS -eq 1 ]]; then
  [[ $DRY_RUN -eq 1 ]] || SWITCHED=1
  run podman stop --time 30 "$NAME"
  run podman rename "$NAME" "$PREVIOUS"
fi
[[ $DRY_RUN -eq 1 ]] || SWITCHED=1

# --restart=always plus `systemctl --user enable podman-restart.service` (or the
# rootful unit) is what brings it back after a reboot; see docs/DEPLOYMENT.md.
run podman run -d --name "$NAME" --restart=always \
  "${COMMON_ARGS[@]}" \
  -p "${HOST_ADDR}:${HOST_PORT}:${APP_PORT}" \
  --label "io.beherouter.deployed-by=deploy.sh" \
  --label "io.beherouter.deploy-id=${DEPLOY_ID}" \
  "$IMAGE"

# ---- 4. verify -------------------------------------------------------------------
log "4/5 verify: $HEALTH_URL"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "    [dry-run] poll $HEALTH_URL for up to ${TIMEOUT}s; require status ok$([[ $ALLOW_DEGRADED -eq 1 ]] && echo ' or degraded')"
else
  wait_healthy "$NAME" || exit 1
fi

if [[ $DEEP -eq 1 ]]; then
  log "    verify: health --deep --json (a credentialed call per surface)"
  if [[ $DRY_RUN -eq 1 ]]; then
    run podman exec "$NAME" beherouter health --deep --json
  else
    deep_health "$NAME" || exit 1
  fi
fi

# ---- 5. done -----------------------------------------------------------------------
DONE=1
if [[ $HAD_PREVIOUS -eq 1 && $KEEP_PREVIOUS -eq 0 ]]; then
  log "5/5 cleanup: removing $PREVIOUS ($PREV_IMAGE_NAME)"
  prev_snapshot="$(registry_source "$PREVIOUS" || true)"
  run podman rm -f "$PREVIOUS"
  if [[ $DRY_RUN -eq 0 ]]; then drop_snapshot "$prev_snapshot"; fi
else
  log "5/5 cleanup: nothing to remove"
  if [[ $HAD_PREVIOUS -eq 1 ]]; then
    echo "    kept $PREVIOUS ($PREV_IMAGE_NAME); 'podman rm $PREVIOUS' when satisfied"
  fi
fi
if [[ $DRY_RUN -eq 1 ]]; then
  log "dry run complete: would deploy $IMAGE as $NAME (previous: $PREV_IMAGE_NAME)"
else
  log "deployed $IMAGE as $NAME (previous: $PREV_IMAGE_NAME)"
fi
