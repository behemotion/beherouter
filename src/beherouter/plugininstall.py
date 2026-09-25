"""Install out-of-tree plugins into a directory the gateway puts on PYTHONPATH.

    python -m beherouter.plugininstall --target /opt/beherouter/plugins acme-crm==0.1.0

Run by the chart's `plugins` init container (`plugins.install` in values.yaml),
so a Kubernetes deployment can add a `beherouter.plugins` entry point without
building an image. `importlib.metadata` scans `sys.path`, so a distribution
under a PYTHONPATH directory is discovered exactly like one in the venv.

⚠️ A BARE `uv pip install --target` IS NOT SAFE HERE, and this module exists
because of it. `--target` resolves against an EMPTY directory, so it installs a
fresh copy of every dependency the plugin shares with the gateway — httpx,
anyio, pydantic, fastmcp — at whatever version the index has today. PYTHONPATH
comes BEFORE site-packages, so those copies would shadow the gateway's own
tested versions: one plugin install silently upgrading the gateway underneath
itself. So:

  1. every distribution the gateway already has is a CONSTRAINT at the
     gateway's exact version — a plugin needing a different one fails the
     install, loudly, before the pod starts;
  2. a distribution the gateway has but no index serves (installed from a path
     or a VCS URL: beherouter itself, the git-pinned beheaxi) is OVERRIDDEN
     away with an impossible marker, since the gateway's copy serves it;
  3. after the install, every copy of a gateway distribution is PRUNED from
     the target, so the directory holds only what the plugin adds.

The index and its credentials come from uv's own environment variables
(`UV_DEFAULT_INDEX`, `UV_INDEX_<NAME>_USERNAME`/`_PASSWORD`), which the chart
sets; nothing credential-shaped passes through argv.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import distributions
from pathlib import Path

# Never offered to an index, whatever its direct_url says.
_ALWAYS_OVERRIDDEN = ("beherouter",)


def normalise(name: str) -> str:
    """PEP 503 normalisation, so `Foo_Bar` and `foo-bar` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def gateway_distributions(exclude: Path | None = None) -> dict[str, tuple[str, bool]]:
    """{normalised name: (version, from_an_index)} for this interpreter's path.

    `from_an_index` is False for a PEP 610 direct-URL install (a local path or
    a VCS URL), which no index can serve and so cannot be a constraint.
    """
    out: dict[str, tuple[str, bool]] = {}
    for dist in distributions():
        location = Path(str(dist.locate_file(""))).resolve()
        if exclude is not None and (location == exclude or exclude in location.parents):
            continue
        name = dist.metadata.get("Name")
        if not name:
            continue
        direct = dist.read_text("direct_url.json")
        out.setdefault(normalise(name), (dist.version, direct is None))
    return out


def requirement_files(have: dict[str, tuple[str, bool]], tmp: Path) -> tuple[Path, Path]:
    constraints = tmp / "constraints.txt"
    overrides = tmp / "overrides.txt"
    pinned, dropped = [], []
    for name, (version, from_index) in sorted(have.items()):
        if from_index and name not in _ALWAYS_OVERRIDDEN:
            pinned.append(f"{name}=={version}")
        else:
            dropped.append(f'{name}; sys_platform == "never"')
    constraints.write_text("\n".join(pinned) + "\n")
    overrides.write_text("\n".join(dropped) + "\n")
    return constraints, overrides


def prune(target: Path, have: dict) -> list[str]:
    """Remove every distribution in `target` that the gateway already has."""
    removed = []
    for info in sorted(target.glob("*.dist-info")):
        name = normalise(info.name[: -len(".dist-info")].rsplit("-", 1)[0])
        if name not in have:
            continue
        record = info / "RECORD"
        if record.exists():
            for line in record.read_text().splitlines():
                rel = line.split(",", 1)[0]
                path = (target / rel).resolve()
                if target in path.parents and path.is_file():
                    path.unlink()
        shutil.rmtree(info, ignore_errors=True)
        removed.append(name)
    # Package directories the RECORDs emptied.
    for d in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return removed


def install(target: Path, specs: list[str], run=subprocess.run) -> dict:
    if not specs:
        return {"target": str(target), "added": [], "shared_with_gateway": []}
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    have = gateway_distributions(exclude=target)
    with tempfile.TemporaryDirectory() as tmp:
        constraints, overrides = requirement_files(have, Path(tmp))
        cmd = [
            "uv", "pip", "install",
            "--python", sys.executable,
            "--target", str(target),
            "--constraints", str(constraints),
            "--overrides", str(overrides),
            *specs,
        ]
        # stdout stays ours (one JSON document); uv's progress goes to stderr.
        proc = run(cmd, stdout=sys.stderr, check=False)
        if proc.returncode != 0:
            raise SystemExit(
                f"plugin install failed (uv exit {proc.returncode}); a plugin whose "
                f"dependencies conflict with the gateway's own versions is refused "
                f"here, before the pod starts"
            )
    shared = prune(target, have)
    added = sorted(
        info.name[: -len(".dist-info")] for info in target.glob("*.dist-info")
    )
    return {"target": str(target), "added": added, "shared_with_gateway": shared}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m beherouter.plugininstall")
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("specs", nargs="*")
    args = parser.parse_args(argv)
    print(json.dumps(install(args.target, args.specs)))


if __name__ == "__main__":
    main()
