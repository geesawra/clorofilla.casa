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

The playbooks target the Fedora 44 `bfg` host. The control machine needs Ansible, Kubernetes/Helm clients, the `kubernetes` and `netaddr` Python packages, and the Galaxy dependencies used by the playbooks.

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

It installs `ansible/collections/requirements.yml` and `ansible/roles/requirements.yml` on the control machine. If you prefer to invoke Galaxy directly, the equivalent commands are:

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

After `1_setup.yml` completes, your kubeconfig is at `~/.kube/config`:

```sh
kubectl get nodes
```

## Immich GPU layout

Immich is entirely Helm-managed. There is no production Docker ML container:

- the Immich chart runs the `v3.1.0-rocm` machine-learning component on `bfg`;
- the ML pod receives Raphael's `/dev/kfd`, `/dev/dri/card1`, and `/dev/dri/renderD129` devices;
- the chart sets `ROCR_VISIBLE_DEVICES=0`, `HSA_OVERRIDE_GFX_VERSION=10.3.0`, and `HSA_USE_SVM=0`;
- the server uses the same Raphael DRM devices for declarative VAAPI transcoding and hardware decode;
- the RX 6950 XT is not mounted into Immich and remains available for LLM workloads.

The temporary Docker container used during the migration was only a GPU probe. It is not managed by Ansible. The chart has `/ping` startup, readiness, and liveness probes for ML; check it with:

```sh
kubectl get pods -n immich
kubectl logs -n immich deployment/immich-machine-learning
kubectl port-forward -n immich service/immich-machine-learning 3003:3003
curl http://127.0.0.1:3003/ping
```

The current bfg validation observed Fedora 44, Raphael as `gfx1036` when the compatibility override is removed, and device groups `video=39` / `render=105`. The `/dev/dri` numbering is hardware enumeration, not a universal identifier; rerun the GPU preflight and revalidate ROCr after kernel, firmware, or hardware changes.

## Migration and cutover

Applying these manifests to a fresh K3s cluster does **not** migrate application state by itself. Before stopping the old cluster:

1. inventory every old PVC/PV and classify it as disposable, NAS-backed static data, dynamically provisioned NFS data, or node-local data;
2. make and test a CloudNativePG backup/restore for both Immich and AudioMuse; a new CNPG `Cluster` otherwise initializes an empty database even when the photo/music files remain on the NAS;
3. preserve or explicitly copy/rebind Home Assistant, Navidrome, Prometheus, Grafana, and any other dynamic-NFS PVC data; local-path databases and caches need separate backups or can be deliberately re-created;
4. deploy bfg and validate pods, NFS mounts, Immich ML `/ping`, a real VAAPI transcode, and application metadata before changing DNS/Traefik cutover;
5. keep the old cluster quiesced but intact until the new cluster passes those checks, with the old kubeconfig retained for rollback and cleanup.

The old-cluster-specific deployment is removed from this repository. The new-host playbooks do not attempt to delete resources from the previous cluster; old-cluster cleanup must be performed explicitly against its retained kubecontext after a successful cutover.

## A note on reproducibility

This repo is mostly to keep me safe, you're not really supposed to deploy this stuff anywhere else, though feel free to draw inspiration from it!
