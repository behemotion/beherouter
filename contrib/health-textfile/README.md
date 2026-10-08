# Scheduled deep health → Prometheus

`/healthz` answers "is the gateway up". It stays **green through a revoked
credential**: `tools/list`, `search_tools` and `describe_tool` are served from
the catalogue cached at attach, so on 2026-07-30 a dead Gitea PAT listed and
searched perfectly while every tool call failed. Only `beherouter health --deep`
calls each backend's probe and touches the credential — this directory runs it
on a schedule and hands the verdict to Prometheus through node_exporter's
**textfile collector**.

```sh
beherouter health --deep --textfile /var/lib/node_exporter/textfile_collector/beherouter.prom
```

## The contract

- **One file**, `beherouter.prom`, replaced atomically on every run (temp file
  in the same directory, then rename — the temp name does not end in `.prom`,
  so the collector never scrapes it half-written). The directory must already
  exist; `--textfile` into a missing one is a usage error (exit 2), never a
  silently created directory nothing scrapes.
- Written on a **red** sweep too, before the exit code (6) is raised, so the
  collector never keeps exporting the last green file.
- Not written at all when the sweep cannot run (unreadable registry, missing
  binary, crash). That case is the **staleness** alert below.
- Every metric is a gauge. The only labels are `surface` (the registry key)
  and, on one family, `state` (a fixed verdict enum). **No identity value is
  ever written** — no subject, no backend answer, no error text: the file is
  usually world-readable and the TSDB keeps it for months.

| Metric | Labels | Meaning |
|---|---|---|
| `beherouter_surface_attach_ok` | `surface` | 1 if the backend attached this sweep |
| `beherouter_surface_probe_configured` | `surface` | 1 if the surface has a credential probe |
| `beherouter_surface_probe_ok` | `surface` | 1 probe succeeded; 0 failed, or attach failed. **Absent** when no probe is configured (absence of evidence is neither green nor red) |
| `beherouter_surface_pinned_missing` | `surface` | pinned tools the backend no longer serves; absent when the catalogue was not seen |
| `beherouter_surface_user_probe_ok` | `surface` | with `--bearer-file`: 1 the probe ran **as that user** and the backend answered as them; 0 rejected / refused / failed / mismatch. Absent when not applicable |
| `beherouter_surface_user_probe_state` | `surface`, `state` | 1 for the per-user verdict: `ok`, `mismatch`, `failed`, `rejected`, `refused`, `not_applicable`, `skipped` |
| `beherouter_surface_healthy` | `surface` | 1 unless the surface is in the sweep's failed list (the same list that sets exit 6) |
| `beherouter_health_ok` | — | 1 if no surface failed |
| `beherouter_health_surfaces` | — | surfaces checked |
| `beherouter_health_last_run_timestamp_seconds` | — | when the sweep finished |

`--surface S` narrows the sweep to one surface; the file then holds only that
surface, so give each narrowed timer its own file name.

### The per-user probe

A green `probe_ok` proves the **deployment** credential only. To prove that a
user's identity reaches the backend as that user, add `--bearer-file` with a
token for a dedicated monitoring identity:

```sh
beherouter health --deep --bearer-file /etc/beherouter/probe-user.jwt \
    --textfile /var/lib/node_exporter/textfile_collector/beherouter.prom
```

`mismatch` — the backend answered as someone else — is the incident this
exists to catch. ⚠️ The token expires: an IdP-issued JWT turns the series to
`rejected` when it does, so mint it from a client-credentials grant in an
`ExecStartPre=` (or an init container) rather than pasting a long-lived one.

## Alert suggestions

```yaml
groups:
  - name: beherouter
    rules:
      - alert: BeherouterBackendUnhealthy
        expr: beherouter_surface_healthy == 0
        for: 10m   # two consecutive 5-minute sweeps
        annotations:
          summary: "beherouter surface {{ $labels.surface }} failed deep health"
          description: "Run `beherouter health --deep --surface {{ $labels.surface }} --json` for the reason."
      - alert: BeherouterCredentialDead
        expr: beherouter_surface_probe_ok == 0 and beherouter_surface_attach_ok == 1
        for: 10m
      - alert: BeherouterPerUserIdentityMismatch
        expr: beherouter_surface_user_probe_state{state="mismatch"} == 1
        # no `for`: a single answer-as-someone-else is the incident
      - alert: BeherouterHealthSweepStale
        # 3 missed 5-minute runs. Silent if the file vanished (the series is
        # then absent, not old), hence the absent() rule below.
        expr: time() - beherouter_health_last_run_timestamp_seconds > 900
      - alert: BeherouterHealthSweepMissing
        expr: absent(beherouter_health_last_run_timestamp_seconds)
        for: 15m
```

Do **not** alert on `beherouter_surface_probe_configured == 0` as an outage —
an unprobed surface is a configuration gap, better reported on a dashboard.

## Running it

- **systemd** — `beherouter-health.service` + `beherouter-health.timer`, every
  5 minutes. Exit 6 (a red sweep) counts as success for the unit, because the
  metrics already carry it; a *failed* unit means the sweep could not run.
- **Kubernetes** — `cronjob.yaml`, a chart-agnostic CronJob that reuses the
  chart's registry ConfigMap and env Secret and writes through a `hostPath`
  into the node_exporter textfile directory of **one pinned node**. If the
  namespace has a default-deny egress NetworkPolicy, allow this Job the same
  egress the gateway pods have. Clusters with a Pushgateway can instead drop
  the hostPath and push the file:
  `python -c "import httpx,sys; httpx.put(sys.argv[1], content=open(sys.argv[2],'rb').read()).raise_for_status()" http://pushgateway:9091/metrics/job/beherouter_health /tmp/beherouter.prom`
  (with `--textfile /tmp/beherouter.prom`; the image has Python and httpx,
  not curl). Pushgateway keeps the last push forever, so the staleness alert
  matters even more there.

Both run the sweep with the **same registry and credentials** the gateway
serves with: the sweep attaches every backend fresh, in its own process, so it
is a black-box check independent of the running gateway — and it needs every
`${VAR}` the registry names.
