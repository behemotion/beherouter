"""Maturity tiers at RUNTIME: what `beherouter plugins` shows and what
`registry-lint` warns about.

The tier itself is declared on the `PluginSpec` and proven OFFLINE, by
`beherouter.testing.plugin_conformance` in a test suite — the gateway does not
run pytest, and what it can check live is already `health --deep`. So nothing
here re-checks evidence for an in-tree plugin: the parametrized test over
`PLUGINS` held the claim before the release was cut.

⚠️ An OUT-OF-TREE plugin was held by nobody's test suite we can see. It may not
show `verified` or above unless every evidence path it cites ships in its own
distribution; otherwise it is shown as `probed` at most, with a note saying why.
A claim we cannot see the evidence for is displayed as the claim it can be
checked down to, not as the claim it makes.
"""

from __future__ import annotations

from .plugins.spec import MATURITY_TIERS, PluginSpec

# The highest tier an out-of-tree plugin may show without resolvable evidence.
UNPROVEN_CAP = "probed"


def tier_rank(tier: str) -> int:
    return MATURITY_TIERS.index(tier)


def _evidence_path(item: str) -> str:
    return item.split("::", 1)[0]


def _distribution_files(module: str) -> tuple[str | None, set[str]]:
    """The distribution that ships `module`, and the files it ships."""
    from importlib.metadata import distribution, packages_distributions

    top = module.split(".", 1)[0]
    names = packages_distributions().get(top) or []
    if not names:
        return None, set()
    dist = distribution(names[0])
    return names[0], {str(f).replace("\\", "/") for f in (dist.files or [])}


def displayed_tier(plugin) -> tuple[str, str | None]:
    """(tier to show, note or None) for a registered `Plugin`."""
    spec = plugin.spec
    module = getattr(plugin.build, "__module__", "") or ""
    if module.startswith("beherouter.") or tier_rank(spec.maturity) <= tier_rank(UNPROVEN_CAP):
        return spec.maturity, None
    try:
        dist, files = _distribution_files(module)
    except (ImportError, OSError):  # PackageNotFoundError included; cap is safe
        dist, files = None, set()
    missing = [e for e in spec.evidence if _evidence_path(e) not in files]
    if dist is not None and spec.evidence and not missing:
        return spec.maturity, None
    where = f"distribution '{dist}'" if dist else "no installed distribution"
    return UNPROVEN_CAP, (
        f"claims '{spec.maturity}', but its evidence does not resolve in {where}"
        + (f" (missing: {', '.join(missing)})" if missing and dist else "")
        + f"; shown as '{UNPROVEN_CAP}' at most"
    )


def raise_hint(spec: PluginSpec) -> str:
    """What would lift a `declared` plugin to the next tier, in one sentence."""
    if spec.requires_entry:
        return (
            "it is generic, so nothing tested this entry's `pinned` or `probe`; "
            "only `health --deep` after the deploy proves them. A curated plugin "
            "would raise it: a default probe whose probe_args validate against a "
            "recorded catalogue (probed), every pin and search alias in that "
            "catalogue (catalogued), an e2e test (verified)"
        )
    need = []
    if not spec.probe:
        need.append("a default `probe`")
    if not any(e.endswith(".json") for e in spec.evidence):
        need.append(
            "a recorded catalogue cited in `evidence` that the probe's arguments validate against"
        )
    if not need:
        need.append("its `maturity` raised to the tier its evidence already supports")
    return "to reach 'probed' it needs " + " and ".join(need)


def lint_warning(surface: str, plugin_name: str, spec: PluginSpec) -> str | None:
    """The `registry-lint` warning for an entry whose plugin is `declared`.

    A WARNING, never a failure: every generic and imported entry is
    `declared` by construction, and refusing them would refuse the feature.
    """
    if spec.maturity != "declared":
        return None
    return (
        f"'{surface}': plugin '{plugin_name}' is maturity 'declared'; "
        f"{raise_hint(spec)}."
    )
