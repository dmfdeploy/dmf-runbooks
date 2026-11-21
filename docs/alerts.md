# Alert runbook page

**Scope:** what each alert in the dmfdeploy monitoring suite means, what to
check first, and which recovery steps the repositories document.

**How you get here:** every alert in the dmf-infra Prometheus role sets
`runbook_url` to this page plus an anchor. The anchor is the lowercase alert
name. Alerts that come in more than one severity share one section. The rules
live in `k3s-lab-bootstrap/roles/base/prometheus/templates/rules/alerts.yaml.j2`
in dmf-infra. A check in this repository fails when a rule points at an anchor
that has no section here.

**Prerequisite:** a working `kubectl` context for the cluster that raised the
alert. Run `kubectl config current-context` before you run anything below.
This page never names a cluster or an environment.

Some alerts have no documented recovery. In those sections, the page says so
and points to the closest document. It does not offer a procedure that no
repository describes.

## Where to start

- The monitoring stack runs in the `monitoring` namespace. Prometheus is
  `deployment/prometheus-server` and Alertmanager is
  `statefulset/prometheus-alertmanager`.
- The **Are we OK?** and **Platform services** Grafana dashboards show the
  current verdict and the probe lanes
  (`k3s-lab-bootstrap/roles/base/grafana/files` in dmf-infra).
- The `195-are-we-ok-verify.yml` playbook reads the rules and probe lanes at
  the source and changes nothing. Run it from the dmf-env repository, like any
  dmf-infra playbook:
  `bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-monitoring/195-are-we-ok-verify.yml`.
- Thresholds are role variables. To change one, set the variable in the
  environment inventory and run the role's playbook again. Do not edit the
  rule template. The defaults are in
  `k3s-lab-bootstrap/roles/base/prometheus/defaults/main.yml`.

## OpenBao

### `openbao-approle-rotation`

Alerts: `OpenBaoAppRoleObserverDown`, `OpenBaoAppRoleMetadataUnavailable`,
`OpenBaoAppRoleRotationDue`, `OpenBaoAppRoleSecretIDExpiryWarning`,
`OpenBaoAppRoleSecretIDExpiryCritical`,
`OpenBaoAppRoleRotationLatestRunFailed`,
`OpenBaoAppRoleScheduledRotationHeartbeatMissing` and
`OpenBaoAppRoleScheduledRotationHeartbeatNeverRecorded`.

**What it means.** The `dmf-promsd` observer reads the rotation record of each
AppRole target and exports it as metrics. `OpenBaoAppRoleObserverDown` means
Prometheus cannot scrape the observer. The other alerts report a missing
record, a SecretID with less than two thirds of its lifetime left (`info`),
less than 25% left (`warning`), less than 10% left (`critical`), a failed
latest run, or no successful scheduled rotation for 26 hours.

**Check first.**

1. Read the observer pod: `kubectl -n monitoring get pods`, then
   `kubectl -n monitoring logs deployment/dmf-promsd`.
2. Check the record contract. The record sits at
   `secret/data/platform/approle-rotation/<target>` and holds the latest run
   outcome and a bounded failure reason code
   (`k3s-lab-bootstrap/docs/openbao-approle-rotation-metadata-v1.md` in
   dmf-infra).

**Recover.** Rotate the SecretIDs again with playbook 115. The playbook
refuses to run when a precondition fails and says what to fix. A second run is
safe, because a target that is not due reports `not_due` and a pending
transaction continues:

```bash
bin/run-playbook.sh <env-name> \
  ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-orchestration/115-approle-rotation.yml \
  -e approle_rotation_target=all -e approle_rotation_actor=<name> \
  -e approle_rotation_reason="<reason>"
```

For the target list, see
`k3s-lab-bootstrap/docs/openbao-approle-lifecycle-registry.md` in dmf-infra.
No repository documents a recovery for `OpenBaoAppRoleObserverDown` beyond the
pod checks in this section.

### `openbao-token-revoker`

Alerts: `OpenBaoRevokerRecordUnreadable`, `OpenBaoRevokerNotScanning` and
`OpenBaoRevokerScanFailing`.

**What it means.** The token revoker ends the root-policy tokens that the
approval executor leaves behind. `OpenBaoRevokerRecordUnreadable`
(`critical`, after 3 minutes) means the audit record is missing, unreadable or
fails its chain check. The revoker then counts no token as recorded and
revokes only executor tokens older than its 5-minute grace period.
`OpenBaoRevokerNotScanning` (`warning`) means no complete scan finished for
5 minutes. `OpenBaoRevokerScanFailing` (`warning`) means the latest scans
failed a login, a listing, a lookup, or a revoke.

**Check first.** Read the log of the revoker pod in the `openbao` namespace:
`kubectl -n openbao get pods`, then
`kubectl -n openbao logs deployment/token-revoker`. The `ALERT` line gives the
reason for an unreadable record. Failed steps have lines that name them. Log
lines hold accessors and never a token. A stopped revoker reports no metrics,
so the last two alerts stay quiet while the Deployment has zero replicas.

**Recover.** No repository documents a recovery procedure. The closest
document is the "Token revoker" section of
`k3s-lab-bootstrap/roles/stack/operator/openbao/README.md` in dmf-infra. It
describes the record, the fail-closed rule and the metrics.

## Host saturation

These alerts use node-exporter series that Prometheus aggregates per host. For
all of them, `kubectl top node` gives the current load.

### `HostMemoryPressure`

**What it means.** `HostMemoryPressure` fires at `warning` when the available
memory ratio of a host stays below `prometheus_alert_mem_avail_warn_ratio`
for 5 minutes. It fires at `critical` below
`prometheus_alert_mem_avail_crit_ratio`. The rule annotations call it the
first link of a chain: memory pressure, swap thrash, load spiral, k3s
restarts.

**Check first.** Run `kubectl top node`, then `kubectl top pod -A` to find
the largest memory users. On the reference rig, an awake AWX held 4 to 5 GB.
Check it with `kubectl -n awx get deploy`.

**Recover.** No repository documents a recovery procedure. The closest
document describes the cause and fix of the original chain: section 1a of
`docs/archive/plans/DMF Constrained-Node k3s Control-Plane Stability Plan 2026-06-21.md`
in the umbrella repository.

### `HostSwapThrashing`

**What it means.** The host swaps in and out more than
`prometheus_alert_swap_io_pages_rate` pages per second for 10 minutes
(`warning`). The rule annotation calls it the signature that came before k3s
restart cycling.

**Check first.** The checks for [`HostMemoryPressure`](#hostmemorypressure)
apply.

**Recover.** No repository documents a recovery procedure. See the document
named under `HostMemoryPressure`.

### `HostHighLoad`

**What it means.** The one-minute load per core on a host stays over
`prometheus_alert_load_per_core` for 10 minutes (`warning`). The rule
annotation notes that on this rig, high load with moderate CPU use points to
memory reclaim or IO wait.

**Check first.** Compare with [`HostMemoryPressure`](#hostmemorypressure) and
[`HostIOWaitHigh`](#hostiowaithigh). Run `kubectl top node`.

**Recover.** No repository documents a recovery procedure.

### `HostIOWaitHigh`

**What it means.** The CPU iowait ratio of a host stays over
`prometheus_alert_iowait_ratio` for 10 minutes (`warning`). Storage is the
bottleneck. The rule annotation names SD-card class storage as the typical
cause on the reference rig.

**Check first.** Run `kubectl top pod -A` and look for storage-heavy
workloads. Check the volumes with `kubectl get pvc -A`.

**Recover.** No repository documents a recovery procedure.

### `HostThermalHot`

**What it means.** The highest thermal-zone temperature on a host stays over
`prometheus_alert_temp_warn_celsius` for 5 minutes (`warning`) or over
`prometheus_alert_temp_crit_celsius` (`critical`). The series exist only on
bare-metal Raspberry Pi nodes. On VM and cloud environments the alert never
fires.

**Check first.** Check [`HostFanMaxed`](#hostfanmaxed) for the same host. The
`critical` annotation says CPU throttling is near or under way.

**Recover.** No repository documents a recovery procedure.
[`HostMemoryPressure`](#hostmemorypressure) lists how to find the largest
load on a node.

### `HostFanMaxed`

**What it means.** The cooling fan of a host has run at its maximum state for
15 minutes (`info`). This signals sustained thermal load on bare-metal
Raspberry Pi nodes only.

**Check first.** See [`HostThermalHot`](#hostthermalhot).

**Recover.** No repository documents a recovery procedure.

### `HostDiskNearFull`

**What it means.** A node filesystem, excluding `tmpfs`, `ramfs`, `overlay`
and `squashfs`, is more than `prometheus_alert_pv_full_threshold` full for
10 minutes (`warning`). The annotation names the device and mount point.

**Check first.** Open a shell on the node named in the alert and run `df -h`
for the mount point in the annotation.

**Recover.** No repository documents a recovery procedure for node
filesystems.

## Workload health

### `NodeDown`

**What it means.** A node has reported `NotReady` for longer than
`prometheus_alert_node_down_for` (`critical`).

**Check first.** Run `kubectl get nodes`, then `kubectl describe node <node>`
for the node in the alert.

**Recover.** No repository documents a recovery procedure for a `NotReady`
node. The closest document explains one known cause, memory exhaustion that
makes k3s restart:
`docs/archive/plans/DMF Constrained-Node k3s Control-Plane Stability Plan 2026-06-21.md`
in the umbrella repository.

### `PodCrashLooping`

**What it means.** A container has been in `CrashLoopBackOff` for longer than
`prometheus_alert_crashloop_for` (`warning`). The annotation names the
namespace, the pod, and the container.

**Check first.** Run `kubectl -n <namespace> describe pod <pod>`, then
`kubectl -n <namespace> logs <pod> -c <container> --previous`.

**Recover.** No repository documents a general recovery. The fix depends on
the workload. For the media path, see
[media-path-recovery.md](runbooks/media-path-recovery.md).

### `PodOOMKilled`

**What it means.** The kernel killed a container for running out of memory,
and the container restarted within the last 30 minutes (`warning`). The
annotation names a limit that is too tight or pressure on the node.

**Check first.** Run `kubectl -n <namespace> describe pod <pod>` and read the
last state of the container. Compare with
[`HostMemoryPressure`](#hostmemorypressure).

**Recover.** No repository documents a recovery procedure.

### `PodNotReady`

**What it means.** A pod has been `Pending` or `Unknown` for 15 minutes
(`warning`). The annotation calls it unschedulable or lost.

**Check first.** Run `kubectl -n <namespace> describe pod <pod>` and read the
events at the end.

**Recover.** No repository documents a recovery procedure.

### `ContainerCPUThrottling`

**What it means.** More than 25% of the CFS periods of a container were
throttled for 15 minutes (`info`). The CPU limit is the constraint. The rule
calls this expected under graceful degradation.

**Check first.** Run `kubectl top pod -A`, and check the CPU limit in the pod
spec of the container.

**Recover.** No repository documents a recovery procedure. The alert is
informational.

### `PersistentVolumeNearFull`

**What it means.** A PersistentVolumeClaim is more than
`prometheus_alert_pv_full_threshold` used for 10 minutes (`warning`). The
annotation says to expand it or free space before the workload goes read-only.

**Check first.** Run `kubectl get pvc -A` and
`kubectl -n <namespace> describe pvc <name>` for the claim in the alert. Role
variables set the size and retention of the monitoring volumes:
`prometheus_storage_size` with `prometheus_retention_size`, and
`loki_storage_size` with `loki_retention`.

**Recover.** No repository documents a procedure to expand a volume. The
`longhorn` role README in dmf-infra is a stub.

## Runaway workloads

### `ContainerLogLineRateHigh`

**What it means.** One container logs more than
`prometheus_alert_log_line_rate_warn_per_min` lines per minute for 5 minutes
(`warning`). A runaway log volume can saturate node CPU and disk.

**Check first.** Run `kubectl -n <namespace> logs <pod> -c <container> --tail=50`
for the container in the annotation. Compare with
[`ContainerCpuOverRequest`](#containercpuoverrequest).

**Recover.** No repository documents a recovery procedure. The fix is in the
workload that logs.

### `ContainerLogLineRateCritical`

**What it means.** The same measure as
[`ContainerLogLineRateHigh`](#containerloglineratehigh), over
`prometheus_alert_log_line_rate_crit_per_min` for 2 minutes (`critical`).

**Check first.** See [`ContainerLogLineRateHigh`](#containerloglineratehigh).

**Recover.** No repository documents a recovery procedure.

### `ContainerLogByteRateHigh`

**What it means.** One container emits more than
`prometheus_alert_log_byte_rate_warn_bytes_per_sec` bytes per second of log
output for 5 minutes (`warning`).

**Check first.** See [`ContainerLogLineRateHigh`](#containerloglineratehigh).

**Recover.** No repository documents a recovery procedure.

### `ContainerLogByteRateCritical`

**What it means.** The same measure as
[`ContainerLogByteRateHigh`](#containerlogbyteratehigh), over
`prometheus_alert_log_byte_rate_crit_bytes_per_sec` for 2 minutes
(`critical`).

**Check first.** See [`ContainerLogLineRateHigh`](#containerloglineratehigh).

**Recover.** No repository documents a recovery procedure.

### `ContainerCpuOverRequest`

**What it means.** One container uses more than
`prometheus_alert_cpu_over_request_warn_ratio` times its declared CPU request
for 10 minutes (`warning`), or more than
`prometheus_alert_cpu_over_request_crit_ratio` times for 5 minutes
(`critical`). Containers without a CPU request are excluded. The alert is the
guard against a runaway container, because nothing else caps the media path.

**Check first.** Run `kubectl top pod -A`, then
`kubectl -n <namespace> logs <pod> -c <container> --tail=50`.

**Recover.** No repository documents a recovery procedure.

### `PromtailLogLimitDropping`

**What it means.** The limit stage of Promtail drops log lines from a pod
(`warning`, after `prometheus_alert_ingestion_drop_for`). The node-protection
guard is engaged. The limit is `promtail_log_line_limit_rate` lines per second
with a burst of `promtail_log_line_limit_burst`.

**Check first.** Find the pod in the annotation, and check
[`ContainerLogLineRateHigh`](#containerloglineratehigh) for its source
container.

**Recover.** No repository documents a recovery procedure. The limit is set
in `k3s-lab-bootstrap/roles/base/promtail/defaults/main.yml` in dmf-infra.

### `LokiPerStreamRateLimitDropping`

**What it means.** Loki discards samples because a stream exceeds the
per-stream limit `loki_per_stream_rate_limit` (`warning`, after
`prometheus_alert_ingestion_drop_for`). The alert exists so that dropped logs
are never silent.

**Check first.** Find the noisy stream with
[`ContainerLogLineRateHigh`](#containerloglineratehigh).

**Recover.** No repository documents a recovery procedure. The limit is set
in `k3s-lab-bootstrap/roles/stack/operator/loki/defaults/main.yml` in
dmf-infra.

### `LokiPerStreamRateLimitDroppingBytes`

**What it means.** The bytes counterpart of
[`LokiPerStreamRateLimitDropping`](#lokiperstreamratelimitdropping). Loki
discards bytes over the per-stream limit (`warning`).

**Check first.** See
[`LokiPerStreamRateLimitDropping`](#lokiperstreamratelimitdropping).

**Recover.** No repository documents a recovery procedure.

## Monitoring stack

### `TargetDown`

**What it means.** Prometheus cannot scrape a target in one of the critical
jobs `prometheus`, `kubernetes-apiservers`, `kubernetes-nodes`,
`kubernetes-nodes-cadvisor` or `dmf-static-probes` for 10 minutes
(`warning`). For `dmf-static-probes`, the blackbox exporter is unreachable.

**Check first.** Read the `instance` and `job` labels in the alert. Run
`kubectl -n monitoring get pods`.

**Recover.** No repository documents a recovery procedure. The playbook
`150-blackbox.yml` deploys the blackbox exporter.

### `AlertmanagerDown`

**What it means.** Prometheus sees no Alertmanager for 5 minutes
(`critical`). The notification path is broken and nothing pages. The
**Are we OK?** verdict still works, because it reads the alerts from
Prometheus directly. The rule exists only when
`prometheus_alertmanager_enabled` is true.

**Check first.** Run
`kubectl -n monitoring get statefulset prometheus-alertmanager` and
`kubectl -n monitoring get pods`.

**Recover.** No repository documents a recovery procedure. To check the
notification path after a repair, run
`playbooks/vertical-monitoring/190-monitoring-verify.yml`. It sends a test
alert through Alertmanager to ntfy.

### `PrometheusNotificationErrors`

**What it means.** Prometheus fails to send alerts to Alertmanager for
10 minutes (`warning`). Notifications are dropped or delayed. The rule exists
only when `prometheus_alertmanager_enabled` is true.

**Check first.** Run
`kubectl -n monitoring logs statefulset/prometheus-alertmanager --all-containers`.
Check the formatter bridge with
`kubectl -n monitoring logs deployment/ntfy-alertmanager-bridge`.

**Recover.** No repository documents a recovery procedure. The role variable
`prometheus_alertmanager_ntfy_url` sets the ntfy topic URL.

### `PrometheusConfigReloadFailed`

**What it means.** The last configuration reload failed (`critical`).
Prometheus runs on a stale configuration, so rule and scrape changes are not
live.

**Check first.** Run
`kubectl -n monitoring logs deployment/prometheus-server --all-containers`.
The rendered rules are in the `prometheus-server` ConfigMap. The header of
`alerts.yaml.j2` says to render the template with test variables and run
`promtool check rules` on the output.

**Recover.** No repository documents a recovery procedure. The playbook
`100-prometheus.yml` renders and applies the configuration again.

### `PrometheusTSDBReloadFailed`

**What it means.** A TSDB block reload failed within the last 3 hours
(`warning`).

**Check first.** Run
`kubectl -n monitoring logs deployment/prometheus-server --all-containers`.

**Recover.** No repository documents a recovery procedure.

### `PrometheusRuleEvalFailures`

**What it means.** Recording or alerting rule evaluations failed in the last
15 minutes (`warning`). The verdict pane and the alert suite may run on missing
series.

**Check first.** Run
`kubectl -n monitoring logs deployment/prometheus-server --all-containers`
and look for rule evaluation errors.

**Recover.** No repository documents a recovery procedure.

## Service discovery pipeline

The pipeline is NetBox, then `dmf-promsd`, then Prometheus service discovery.

### `PromsdDown`

**What it means.** The liveness probe of `dmf-promsd` (`/healthz`) has failed
for 5 minutes (`critical`). Prometheus runs on its last known target sets.

**Check first.** Run `kubectl -n monitoring get deployment dmf-promsd` and
`kubectl -n monitoring logs deployment/dmf-promsd`.

**Recover.** No repository documents a recovery procedure. The playbook
`160-promsd.yml` deploys the service.

### `PromsdStale`

**What it means.** The readiness probe (`/readyz`) has failed for 10 minutes
while the service is alive (`warning`). Its NetBox snapshot is older than the
staleness threshold. NetBox is unreachable or the refresh loop is stuck. The
rule stays quiet while [`PromsdDown`](#promsddown) applies.

**Check first.** Read `/healthz`. It holds `snapshot_age_seconds` and
`last_error`. Reach it with
`kubectl -n monitoring port-forward svc/dmf-promsd 8000:8000`. If `last_error`
names lane tags, the service cannot see them in NetBox.

**Recover.** No repository documents a recovery procedure. The README of
dmf-promsd says the service keeps serving the last good lanes and reports
not ready once they go stale.

### `NetBoxUnreachable`

**What it means.** The probe of the in-cluster NetBox service has failed for
5 minutes (`critical`). NetBox is the monitoring source of truth, so discovery
goes stale.

**Check first.** Run `kubectl -n netbox get pods`.

**Recover.** No repository documents a recovery procedure. The closest
document is `k3s-lab-bootstrap/docs/netbox-deployment-notes.md` in dmf-infra.

### `DiscoveredTargetsZeroOrStale`

**What it means.** The NetBox-driven scrape jobs produce no targets for
15 minutes (`warning`). NetBox holds no monitoring-enabled objects, or
discovery broke without an error.

**Check first.** Check [`PromsdStale`](#promsdstale), then read
`source_counts` in the `/healthz` output of `dmf-promsd`.

**Recover.** No repository documents a recovery procedure.

## Connectivity

The blackbox exporter probes these lanes. The `195-are-we-ok-verify.yml`
playbook requires every default probe to succeed.

### `InternetUnreachable`

**What it means.** ICMP probes to every public anchor failed for 5 minutes
(`critical`). The node has no internet path, or the blackbox exporter lost
the `NET_RAW` capability. Check [`TargetDown`](#targetdown) for the second
case.

**Check first.** Run `kubectl -n monitoring get pods` and read the probe
results in the **Are we OK?** dashboard.

**Recover.** No repository documents a recovery procedure.

### `InternetHighLatency`

**What it means.** The 10-minute average round-trip time to an internet
anchor is over 150 ms for 15 minutes (`warning`). The threshold is a first
guess.

**Check first.** Compare with [`HostHighLoad`](#hosthighload) on the same
node.

**Recover.** No repository documents a recovery procedure.

### `IngressDown`

**What it means.** The TCP probe to the Traefik service
(`traefik.kube-system.svc.cluster.local:443`) failed for 5 minutes
(`critical`). Every public surface of the platform is dark.

**Check first.** Run `kubectl -n kube-system get pods` and
`kubectl -n kube-system get svc traefik`.

**Recover.** No repository documents a recovery procedure. The closest
document is `k3s-lab-bootstrap/roles/base/ingress/README.md` in dmf-infra.
It describes how the role selects the ingress provider and patches Traefik.

### `DNSDown`

**What it means.** The DNS probe against CoreDNS
(`kube-dns.kube-system.svc.cluster.local:53`) failed for 5 minutes
(`critical`). In-cluster name resolution is broken, and the annotation warns
of cascading pod failures.

**Check first.** Run `kubectl -n kube-system get pods` and look at the
CoreDNS pods.

**Recover.** No repository documents a recovery procedure.

### `DNSSlow`

**What it means.** The 10-minute average CoreDNS lookup time is over
0.5 seconds for 10 minutes (`warning`). The annotation calls this typical
under node saturation.

**Check first.** Run `kubectl top node` and compare with
[`HostMemoryPressure`](#hostmemorypressure).

**Recover.** No repository documents a recovery procedure.

### `GatewayUnreachable`

**What it means.** The ICMP probe to the LAN gateway failed for 5 minutes
(`warning`). The lane has no default target, so the alert matches nothing until
an environment adds a target with `prometheus_static_probe_targets` in its
inventory. Environments in the cloud have no LAN lane.

**Check first.** Read the probe result in the **Are we OK?** dashboard and
compare with [`InternetUnreachable`](#internetunreachable).

**Recover.** No repository documents a recovery procedure.

### `GatewayHighJitter`

**What it means.** The round-trip time to the LAN gateway varies by more than
50 ms over 10 minutes, for 15 minutes (`info`). The local link is unstable.
The lane note under [`GatewayUnreachable`](#gatewayunreachable) applies.

**Check first.** See [`GatewayUnreachable`](#gatewayunreachable).

**Recover.** No repository documents a recovery procedure.
