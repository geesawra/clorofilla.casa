# AGENTS.md

Ensure that all practices and instructions described by
https://raw.githubusercontent.com/ansible/ansible-creator/refs/heads/main/docs/agents.md
are followed.

---

## Repo Structure

```
clorofilla.casa/
├── ansible/
│   ├── 1_setup.yml          # Node provisioning (Tailscale, K3s)
│   ├── 2_services.yml       # K8s workload deployments (ordered by filename)
│   ├── 3_monitoring.yml     # Monitoring stack (Docker node_exporter, Prometheus, Grafana)
│   ├── ansible.cfg
│   ├── collections/         # Ansible Galaxy collection requirements and local collection
│   ├── roles/               # Ansible Galaxy role requirements
│   ├── group_vars/
│   │   └── all/
│   │       ├── vars.yml     # Plain variables
│   │       └── vault.yml    # Encrypted secrets (ansible-vault)
│   ├── inventory/
│   │   └── hosts.yml        # Inventory — target node: bfg
│   ├── k8s/                 # Kubernetes manifests (*.yml) and Helm values
│   │   ├── monitoring/      # Monitoring manifests
│   │   │   ├── prometheus.yml            # Local Prometheus server
│   │   │   └── kube-state-metrics-values.yml
│   │   ├── audiomuse-ai-ingress.yml
│   │   ├── audiomuse-ai-values.yml
│   │   ├── audiomuse-database.yml
│   │   ├── ha.yml
│   │   ├── immich.yml
│   │   ├── immich_values.yml
│   │   ├── navidrome.yml
│   │   ├── reverse-proxy.yml
│   │   ├── traefik_porkbun.yml
│   │   ├── transmission.yml
│   │   ├── transmission_others.yml
│   │   └── trmnl.yml
│   ├── PLAN.md              # Architecture plans & decisions
│   └── AGENTS.md            # This file
├── .gitignore
└── README.md
```

## Commit Style

Use the format `*: <description>` for broad changes, or `<scope>: <description>` for targeted changes.

```
*: add audiomuse, navidrome
ansible/k8s: add trmnl terminus server
k8s: use approved transmission versions
ansible: setup powertop with autotune
*: revert traefik changes, something went wrong and this config just works (tm)
*: reverse proxy some internal services, auto redirect traefik to tls always
ansible: torrents for everybody else
k8s: add matter server to home assistant pod
```

## Environment

- **OS**: Fedora 44
- **Orchestration**: K3s (single-node, bfg)
- **GPU acceleration**: AMD DRA assigns the RX 6950 XT to Immich ML and the Raphael integrated GPU to Immich VAAPI video transcoding
- **Storage**: NAS at `192.168.1.155` (`/mnt/fast/1tb/services`)
- **Networking**: Tailscale only — no public exposure
- **DNS**: PorkBun, pointing to Tailscale IPs
- **TLS**: Let's Encrypt via Traefik
- **Secrets**: Ansible Vault

## Monitoring Stack

Deployed via `ansible/3_monitoring.yml`. Components:

| Component | Where | Port | Purpose |
|-----------|-------|------|---------|
| **node_exporter** | Docker container on node | 9100 | Host metrics (CPU, RAM, disk, hwmon temps/fans/voltages) |
| **kube-state-metrics** | K8s Deployment in `monitoring` namespace | 8080 | K8s object state (pods, deployments, PVCs, nodes) |
| **Prometheus** | K8s Deployment in `monitoring` namespace | 9090 | Scrapes node_exporter, kube-state-metrics, and the K3s kubelet |

### Prometheus Config (`k8s/monitoring/prometheus.yml`)

The local Prometheus server scrapes three targets:
- `bfg:9100` — node_exporter
- `kube-state-metrics.monitoring.svc.cluster.local:8080` — kube-state-metrics
- `bfg:10255` — k3s kubelet (read-only, cadvisor metrics)

Prometheus data is persisted in the `prometheus-data` PVC and exposed internally through the Prometheus IngressRoute.

### Grafana Dashboards

Recommended dashboards to import into your existing Grafana:

| Dashboard | ID | Purpose |
|-----------|----|---------|
| Kube-State-Metrics Overview | 25091 | High-level cluster view with drill-down |
| kube-state-metrics-v2 | 13332 | KSM v2-specific metrics |
| Node Exporter | 11095 | Full host overview |
| Node Temperatures | 15202 | CPU temps, fan speeds, voltages (hwmon) |
| Prometheus | 14074 | Agent health |

## Ansible Best Practices

### Playbooks

- **Order matters**: `1_setup.yml` → `2_services.yml` → `3_monitoring.yml`
- Each playbook is self-contained and idempotent
- Use `tags` liberally for selective runs: `ansible-playbook 3_monitoring.yml --tags monitoring`
- Use `when: not ansible_check_mode` for tasks that modify remote state (docker run, helm, k8s apply)

### Kubernetes Deployments

- **Direct manifests** (`kubernetes.core.k8s` with `src:`): Use for static YAML that doesn't need Jinja2 rendering. These are applied as-is by kubectl.
- **Jinja2 templates** (`ansible.builtin.template` → `kubernetes.core.k8s` with rendered path): Use when vault secrets or variables need to be injected into the manifest. The `.j2` file is rendered to `/tmp/` first, then applied.
- **Helm charts** (`kubernetes.core.helm`): Use for charts from Helm repos (kube-state-metrics, etc.)

### Secrets

- All secrets go in `group_vars/all/vault.yml`
- Use `ansible-vault edit group_vars/all/vault.yml` to add/edit secrets
- Never put secrets in plain `vars.yml` or commit them
- Reference vault vars with `{{ vault_var_name }}` in templates

### Docker on Nodes

- Docker is installed idempotently by the monitoring playbook if missing on the Fedora bfg node
- node_exporter runs as a privileged Docker container on the node (not in K8s) — it needs host access to `/sys`, `/proc`, `/dev`
- Immich ML is not Docker-managed; AMD DRA/CDI assigns it the RX 6950 XT, while the Immich server receives Raphael for VAAPI

### K8s Manifest Conventions

- Each service gets its own file in `ansible/k8s/`
- Helm values go in `*_values.yml` files
- Monitoring manifests go in `ansible/k8s/monitoring/`
- Namespaces are created inline in playbooks (no separate namespace manifests)

## Common Pitfalls

### Blackbox Exporter v0.28+

The `fail_if_header_matches` field changed from a plain string to an object format:
```yaml
# Old (pre-0.28)
fail_if_header_matches:
  - "X-Blacklisted: .+"

# New (0.28+)
fail_if_header_matches:
  - header: X-Blacklisted
    regexp: ".+"
```

Many old fields were removed in v0.28: `preferred_http_version`, `get_body_as`, `fail_if_ssl_verifies_fails`, `ip_version_pref`, `pushgateway_port`, etc. Use the [v0.28 CONFIGURATION.md](https://github.com/prometheus/blackbox_exporter/blob/v0.28.0/CONFIGURATION.md) as reference.

### Immich Backup Restore with CloudNativePG

Immich's built-in restore executes the backup with the configured `app` database role. Automatic backups contain `DROP EXTENSION`, `CREATE EXTENSION`, and `COMMENT ON EXTENSION` statements, but the steady-state CloudNativePG `app` role is intentionally not a superuser.

For a restore, temporarily set `Cluster.spec.enableSuperuserAccess: true`, use the generated `immich-database-superuser` credential to run `ALTER ROLE app WITH SUPERUSER`, and restore the original backup through **Administration → Maintenance**. The generated superuser secret can contain `dbname: "*"`; connect to the database named by `immich-database-app` instead.

Do not strip only extension DDL from the dump. Immich's restore cleanup drops extension-owned objects before processing the dump, which can leave required objects such as the `public.unaccent` text-search dictionary missing. After every restore attempt, run `ALTER ROLE app WITH NOSUPERUSER`, set `enableSuperuserAccess: false`, confirm the generated superuser secret is deleted, and run `immich-admin schema-check`.

Media files and SQL state must match. Managed uploads use `/data/upload/<owner UUID>` and are not safely recoverable by scanning that directory as an external library. Preserve files newer than the selected SQL backup separately and upload them normally after restoration.

### Port Conflicts

Prometheus listens on port 9090 and node_exporter listens on port 9100. Always check for port conflicts before deploying.

### Jinja2 in K8s Manifests

`kubernetes.core.k8s` with `src:` does **not** render Jinja2 templates. If you need variable interpolation, use `ansible.builtin.template` to render the `.j2` file first, then apply the rendered output.

## Running Ansible

```bash
# Full setup
ansible-playbook ansible/1_setup.yml -i ansible/inventory/hosts.yml

# Deploy services
ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml

# Deploy monitoring
ansible-playbook ansible/3_monitoring.yml -i ansible/inventory/hosts.yml

# Selective tags
ansible-playbook ansible/3_monitoring.yml -i ansible/inventory/hosts.yml --tags monitoring
```

## Kubectl Quick Reference

```bash
# Check pod status
kubectl get pods -n monitoring

# Check logs
kubectl logs -n monitoring -l app=prometheus --tail=20

# Force restart (e.g., after config changes)
kubectl rollout restart deployment/prometheus -n monitoring

# Port-forward for debugging
kubectl port-forward -n monitoring svc/kube-state-metrics 8080:8080
```

## Grafana Cloud Credentials

Stored in vault as `grafanacloud_prometheus_*`. The agent uses these for `remote_write` with `basic_auth`. Do not hardcode these anywhere — always reference via vault variables.
