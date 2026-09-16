# clorofilla.casa IaC

This is my first attempt at writing IaC """code""" for my home server.

It's a very basic Ansible-based setup:
- One node:
  - `bfg`: Fedora 44 with an AMD Ryzen 9 7900X, RX 6950 XT, and Raphael integrated GPU
- service orchestration is managed by K3s
- storage is handled by my NAS
- everything is only accessible either through local IPs or Tailscale
- TLS managed by LetsEncrypt
- DNS handled by PorkBun, using Tailscale IPs
- all secrets are encrypted with Ansible Vault

Playbooks will take care of enrolling the system in the Tailscale Tailnet 
attached to the provided API key.

## Prerequisites and dependency installation

The playbooks target the Fedora 44 `bfg` host. The control machine needs Ansible, Kubernetes/Helm clients, the `kubernetes` and `netaddr` Python packages, `ansible-lint`, and the Galaxy dependencies used by the playbooks.

### Install control-machine tools

Install these with your operating system's package manager:

- Ansible Core 2.17+ (the local collection metadata requires this version)
- `kubectl` — [install guide](https://kubernetes.io/docs/tasks/tools/)
- `helm` and a `helm3` binary at `/usr/bin/helm3` (the AudioMuse and monitoring tasks use that explicit path)
- `git`, for Galaxy/source dependency installation
- `just`, for the repository's bootstrap and deployment shortcuts

### Install all Ansible collections, roles, and Python dependencies

The repository declares every collection and role dependency in tracked requirements files. Install [`just`](https://github.com/casey/just) with your operating system's package manager, then run the bootstrap target once from the repository root:

```sh
just bootstrap
```

It installs pinned `ansible-lint`, the Python client dependencies, `ansible/collections/requirements.yml`, and `ansible/roles/requirements.yml` on the control machine. Run the complete lint and syntax-check suite with:

```sh
just check
```

If you prefer to invoke Galaxy directly, the equivalent collection and role commands are:

```sh
ansible-galaxy collection install -r ansible/collections/requirements.yml
ansible-galaxy role install -r ansible/roles/requirements.yml
```

This installs:

| Dependency | Type | Used by |
|---|---|---|
| `kubernetes.core` | Galaxy collection | Kubernetes and Helm modules |
| `artis3n.tailscale` | Galaxy collection; provides `artis3n.tailscale.machine` | Tailscale setup |
| `k3s.orchestration` | Git-backed Ansible collection from `k3s-io/k3s-ansible`; provides `k3s.orchestration.site` | K3s installation |
| `ansible.posix` | Galaxy collection | POSIX modules and K3s collection dependency |
| `community.general` | Galaxy collection | K3s collection dependency |
| `exploide.dnf-automatic` | Galaxy role | Fedora automatic updates |

The K3s and Tailscale entries are collections even though the playbooks consume a role (`artis3n.tailscale.machine`) and an imported playbook (`k3s.orchestration.site`) from them. The K3s collection is installed from Git because it is not currently resolvable as a normal Galaxy release. Do not install either with `ansible-galaxy role install`.

### Secrets and host access

Create an Ansible Vault password file at `.vault_password` and provide a Tailscale auth key through `TAILSCALE_KEY`. The target host must be reachable over SSH by the user configured in `ansible/ansible.cfg` (`geesawra`) and that user must be able to use `sudo`.

```sh
printf '%s\n' 'your-vault-password' > .vault_password
chmod 600 .vault_password
# Do not commit this file; it is already ignored by the repository.
```

## How do I deploy this thing

Suppose you have the Fedora `bfg` host freshly configured with SSH keys, run playbooks in order:

```sh
# 0 — Install Galaxy collections and roles on the control machine
just bootstrap

# 1 — System init, Tailscale, and K3s
TAILSCALE_KEY='tskey-your-API-key-here' ansible-playbook ansible/1_setup.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password

# 2 — Services (K8s workloads)
ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password

# 3 — Monitoring (Docker node_exporter, Prometheus, and Grafana)
ansible-playbook ansible/3_monitoring.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
```
Lidarr is deployed in the existing `torrents` namespace at
`https://lidarr.clorofilla.casa`. Its configuration is persisted on the NAS;
configure the Redacted indexer and any download clients manually in the
Lidarr web UI. Jackett remains in place.

To deploy only Lidarr:

```sh
ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml \
  --tags lidarr --vault-password-file .vault_password
```

After `1_setup.yml` completes, your kubeconfig is at `~/.kube/config`:

```sh
kubectl get nodes
```

## Immich GPU layout

Immich is entirely Helm-managed. There is no production Docker ML container:

- AMD GPU DRA driver v1.0.1 publishes both GPUs to Kubernetes as individually selectable devices;
- the Immich chart runs the `v3.1.0-rocm` machine-learning component on `bfg`;
- ML requests PCI device ID `0x73a5` and receives the RX 6950 XT;
- the server requests PCI device ID `0x164e` and receives the Raphael iGPU for VAAPI transcoding and hardware decode;
- both Deployments use `maxSurge: 0`, because each DRA claim is exclusive and no spare GPU exists during a rollout;
- DRA injects `/dev/kfd` and only the selected GPU's current DRM card/render nodes through CDI, so `/dev/dri` enumeration changes do not alter the intended mapping.

The DRA chart is designed for Kubernetes 1.32 or newer and this deployment uses the stable `resource.k8s.io/v1` API available in K3s 1.35. The Immich chart renders `/ping` startup, readiness, and liveness probes for its ML Deployment; check it with:

```sh
kubectl get pods -n immich
kubectl logs -n immich deployment/immich-machine-learning
kubectl port-forward -n immich service/immich-machine-learning 3003:3003
curl http://127.0.0.1:3003/ping
```

### One-time migration from the legacy AMD device plugin

Do this only when changing an existing deployment from `amd.com/gpu` limits to DRA. Stop both legacy GPU workloads before removing the old plugin so the old and new allocation systems can never assign the same physical GPU concurrently:

```sh
kubectl delete deployment \
  --namespace immich \
  immich-server immich-machine-learning \
  --ignore-not-found \
  --wait=true

kubectl wait \
  --namespace immich \
  --for=delete pod \
  --selector='app.kubernetes.io/name in (server,machine-learning)' \
  --timeout=2m

helm uninstall amd-gpu \
  --namespace kube-system \
  --wait \
  --timeout 2m

ansible-playbook ansible/2_services.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags immich \
  --vault-password-file .vault_password
```

The deletion removes only the two Helm-managed Deployments and their pods; the next Helm upgrade recreates them. It does not delete the Immich database, Valkey, Services, PVCs, or library data. The steady-state Ansible configuration intentionally does not uninstall the legacy release.

The current bfg validation observed Fedora 44, Raphael as `gfx1036`, RX 6950 XT as `gfx1030`, and device groups `video=39` / `render=105`. The `/dev/dri` numbering is hardware enumeration, not a universal identifier; DRA selects the GPUs by stable PCI device IDs `0x164e` and `0x73a5`. Rerun the GPU preflight and revalidate the claims after kernel, firmware, or hardware changes.

`ResourceClaimTemplate.spec` is immutable. If a selector or allocation policy changes, create a new versioned template name, update `immich_values.yml` to reference it, deploy the workloads, and remove the old unused template afterward.

## Migration and cutover

Applying these manifests to a fresh K3s cluster does **not** migrate application state by itself. Before stopping the old cluster:

1. inventory every old PVC/PV and classify it as disposable, NAS-backed static data, dynamically provisioned NFS data, or node-local data;
2. make and test a CloudNativePG backup/restore for both Immich and AudioMuse; a new CNPG `Cluster` otherwise initializes an empty database even when the photo/music files remain on the NAS;
3. preserve or explicitly copy/rebind Home Assistant, Navidrome, Prometheus, Grafana, and any other dynamic-NFS PVC data; local-path databases and caches need separate backups or can be deliberately re-created;
4. deploy bfg and validate pods, NFS mounts, Immich ML `/ping`, a real VAAPI transcode, and application metadata before changing DNS/Traefik cutover;
5. keep the old cluster quiesced but intact until the new cluster passes those checks, with the old kubeconfig retained for rollback and cleanup.

The old-cluster-specific deployment is removed from this repository. The new-host playbooks do not attempt to delete resources from the previous cluster; old-cluster cleanup must be performed explicitly against its retained kubecontext after a successful cutover.

### Restoring an Immich backup into CloudNativePG

Immich's automatic `.sql.gz` backups contain database metadata only. Preserve the matching `/data/upload`, `/data/library`, and external-library files before restoring. A fresh database can appear healthy while showing an incomplete timeline if the media volumes survived but the old database was not restored. A strong indicator is an old owner UUID under `/data/upload/<uuid>` that does not match the current Immich user's UUID.

Use **Administration → Maintenance → Restore database backup** rather than importing managed upload directories as an external library. The built-in restore creates a restore point, restores in a transaction, runs migrations, performs health checks, and rolls back on failure.

The CloudNativePG application role is deliberately not a superuser, but an Immich backup drops and recreates PostgreSQL extensions such as `earthdistance`, `unaccent`, `vector`, and `vchord`. Temporarily elevate the application role for the restore:

```sh
# 1. Enable a temporary postgres network credential.
kubectl patch cluster immich-database \
  --namespace immich \
  --type merge \
  --patch '{"spec":{"enableSuperuserAccess":true}}'

# 2. Wait for secret/immich-database-superuser, read its connection fields,
#    and connect as postgres to the application database.
#    The secret's dbname may be "*"; use the dbname from
#    secret/immich-database-app instead.
kubectl exec --namespace immich immich-database-1 --container postgres -- \
  env PGPASSWORD="$POSTGRES_PASSWORD" \
  psql --host "$POSTGRES_HOST" --username postgres --dbname "$APP_DATABASE" \
  --set ON_ERROR_STOP=on \
  --command 'ALTER ROLE app WITH SUPERUSER;'
```

Restore the **original, unmodified** `.sql.gz` file through the Maintenance page. Do not remove only `CREATE EXTENSION` statements: Immich's restore cleanup first drops extension-owned objects, and the restore can then fail on missing objects such as `public.unaccent`.

Immediately after success or failure, revoke the temporary privileges:

```sh
kubectl exec --namespace immich immich-database-1 --container postgres -- \
  env PGPASSWORD="$POSTGRES_PASSWORD" \
  psql --host "$POSTGRES_HOST" --username postgres --dbname "$APP_DATABASE" \
  --set ON_ERROR_STOP=on \
  --command 'ALTER ROLE app WITH NOSUPERUSER;'

kubectl patch cluster immich-database \
  --namespace immich \
  --type merge \
  --patch '{"spec":{"enableSuperuserAccess":false}}'
```

CloudNativePG removes the generated `immich-database-superuser` secret after superuser access is disabled. Verify the final state and restored schema:

```sh
kubectl get cluster immich-database --namespace immich \
  --output=jsonpath='{.status.phase}{" superuserAccess="}{.spec.enableSuperuserAccess}{"\n"}'

kubectl exec --namespace immich deployment/immich-server -- \
  immich-admin schema-check
```

Expected steady state: the cluster is healthy, `enableSuperuserAccess` is `false`, the `app` role has `rolsuper = false`, the generated superuser secret is absent, migrations are current, and no schema drift is detected. Preserve separately any media written after the selected backup timestamp and upload it normally after restoration; orphaned files inside Immich-managed storage are not discovered automatically.

## A note on reproducibility

This repo is mostly to keep me safe, you're not really supposed to deploy this stuff anywhere else, though feel free to draw inspiration from it!
