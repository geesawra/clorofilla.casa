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
- `helm` and a `helm3` binary at `/usr/bin/helm3` (the monitoring tasks use that explicit path)
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

## Homepage service directory

The cluster service directory is available at
[`https://home.clorofilla.casa`](https://home.clorofilla.casa). The Homepage
deployment discovers services from Traefik `IngressRoute` annotations. Every
route in the repository opts in with `gethomepage.dev/enabled: "true"`, `href`,
`name`, and `group`; new routes need the same metadata. The dashboard includes
the service links, monitoring tools, infrastructure proxies, and itself.
Internal-only services without a Traefik route remain unlisted.

Homepage's service account can read Traefik `IngressRoute` resources cluster
wide, but cannot read pods, nodes, or secrets. The page follows the cluster's
existing Tailscale-only access model. Deploy or update it independently with:

```sh
ansible-playbook ansible/2_services.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags homepage \
  --vault-password-file .vault_password
```
Lidarr is deployed in the existing `torrents` namespace at
`https://lidarr.clorofilla.casa`. Its configuration is persisted on the NAS;
configure the Redacted indexer and any download clients manually in the
Lidarr web UI. Jackett remains in place.

For indexers that require Cloudflare challenges, FlareSolverr runs in a
separate pod with an internal-only Service. In Jackett settings, set
**FlareSolverr API URL** to
`http://flaresolverr-service.torrents.svc.cluster.local:8191` and leave
**FlareSolverr Max Timeout (ms)** at its default. The solver has no external
route; configuring the URL in Jackett is required after deployment. Deploy
both with the `jackett` tag.

To deploy only Lidarr:

```sh
ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml \
  --tags lidarr --vault-password-file .vault_password
```

After `1_setup.yml` completes, your kubeconfig is at `~/.kube/config`:

```sh
kubectl get nodes
```

## Monitoring and Grafana

Prometheus scrapes kubelet and cAdvisor over verified HTTPS on `bfg:10250`, using
a dedicated ServiceAccount restricted to reading metrics for `bfg`. The old
unauthenticated port 10255 is not used. Container CPU, memory, network, and
filesystem metrics come from a separate cAdvisor job.

Grafana's initializer reads the existing Prometheus datasource UID from its
SQLite database without changing the database, and preserves it in provisioning
and downloaded dashboards. New installations use UID `prometheus`. Downloaded
dashboards are parsed and their Prometheus datasource references normalized
before file provisioning; failed downloads or invalid JSON stop initialization
rather than silently installing broken dashboards. Dashboard JSON and provider
configuration use separate directories. Downloaded revisions still track the
upstream `latest` version, so a future dashboard can change its metric queries.

```sh
ansible-playbook ansible/3_monitoring.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags monitoring \
  --vault-password-file .vault_password
```

Content checksums roll out Prometheus/Grafana when their configuration changes.
Prometheus uses a recreate rollout because its host-network port cannot be shared
by two replicas; scraping briefly pauses during replacement. Existing Prometheus
and Grafana PVCs are preserved. This command also runs the monitoring playbook's
existing node-exporter and kube-state-metrics tasks.

After deployment, check Prometheus targets: `node-exporter`, `kube-state-metrics`,
`k3s-kubelet`, and `kubelet` (cAdvisor) should be up. Grafana dashboard queries
should use the provisioned Prometheus datasource. If HTTPS scraping reports 403,
check kubelet webhook authorization and the `prometheus-kubelet-metrics` RBAC
binding; do not bypass TLS or grant node-proxy access as a workaround.

## Host power profile

The setup playbook checks for `/usr/sbin/tuned-adm` on `bfg`. If it is installed,
it enables and starts TuneD, verifies that `balanced-battery` is available, and
selects that profile only when it is not already active. This is the privileged
equivalent of `sudo tuned-adm profile balanced-battery`. If TuneD is absent, the
play is skipped without installing it; a missing profile fails explicitly.

```sh
ansible-playbook ansible/1_setup.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags tuned \
  --vault-password-file .vault_password
```

The profile-changing commands are skipped in Ansible check mode. This selects
`balanced-battery` explicitly; it does not automatically switch profiles when
AC power changes.

## Immich media on HDD, previews and scratch on SSD

The split is **enabled and deployed** as of 2026-10-15 (Helm revision 16).
`immich_hdd_storage_enabled: true` in `ansible/group_vars/all/vars.yml`
adds `immich_hdd_storage.yml` and layers `immich_hdd_values.yml` after the normal
Helm values. The playbook first requires a nonempty
`/mnt/main/data/photos/.immich-hdd-ready` marker on `truenas`; create that
marker only after completing the frozen-copy verification and NFS export below.
The marker records an operator's verification; it does not verify the media itself.

The HDD dataset is `main/data/photos`, mounted at `/mnt/main/data/photos`,
under the existing `main/data` parent. The copy script creates a new leaf
dataset and refuses to overwrite an existing destination.
New HDD PVs/PVCs have distinct names, explicit bindings, and `Retain` policies;
the existing SSD resources remain available for rollback. Static NFS capacity
requests are Kubernetes binding metadata, not physical reservations or quotas.

| Container path / content | Storage after cutover |
|---|---|
| `/data` (managed originals and database backup files) | NAS HDD: `photos/ImmichData` |
| `/existing-uploads` (read-only) | NAS HDD: `photos/PreviousImmichUploads` |
| `/external` (read-only) | NAS HDD: `photos/CameraPhotos` |
| `/data/thumbs` | Existing SSD claim, `ImmichData/thumbs` subdirectory |
| `/data/encoded-video` | Existing SSD claim, `ImmichData/encoded-video` subdirectory |
| `/data/profile` | Existing SSD claim, `ImmichData/profile` subdirectory |
| `/tmp` | Disk-backed `emptyDir` on bfg's local NVMe; discarded with the pod |
| ML `/cache` | Existing PVC on NAS SSD services dataset |
| PostgreSQL | Existing `local-path` PVC on bfg's local NVMe |

Container paths and the two external-library read-only mounts stay the same.
No database path rewrite, restore, reimport of existing assets, or thumbnail
regeneration is part of the storage cutover. This follows Immich's
[custom-location layout](https://docs.immich.app/guides/custom-locations/).

### Observed baseline, 2026-10-15

Read-only inspection found 679.5 GiB in the photo dataset and about 214 GiB
available on `fast`. Major consumers were CameraPhotos (210.8 GiB), previous
uploads (197.4 GiB), encoded videos (145.7 GiB), managed uploads (65.6 GiB),
thumbnails (22.0 GiB), and `ele30.zip` (30.8 GiB). `main` had about 6.8 TiB
available. These are observations, not limits or expected future sizes.

The source photo dataset uses NFSv4 ACLs with passthrough inheritance, while
`main/data` uses POSIX ACLs. The newly prepared `main/data/immich` parent already
has matching NFSv4 ACL properties. Use ZFS replication for the initial full dataset
copy so ACLs, ownership, extended attributes, and hidden `.immich` markers
survive. A plain `rsync -a` copy would not preserve those NFSv4 ACLs. The initial
replica includes generated files; SSD mounts cover those copies after cutover,
and redundant HDD copies can be removed after the rollback period.

### Prepare and copy while Immich stays online

The following commands are a **future migration procedure**, not a deployment
performed by this repository. Confirm fresh capacities, healthy pools, and a
usable independent media/database backup before starting. Keep Immich and its
chart at their pinned versions during the migration.

On bfg, save the current Helm values, release revision, deployment, and PVC/PV
mapping in a private cutover directory. Record asset counts and representative
working photos/videos from both external libraries. Confirm which user will own
the later GoPro upload. Pause other NAS workflows that could write these files
during the final cutover.

The standalone script [scripts/copy_immich_storage.py](scripts/copy_immich_storage.py)
uses only Python 3.11+, ZFS, and rsync, all already present on the NAS. It accepts
any destination pool/parent; only the new leaf dataset is created. It checks
pool health, space, source/destination identity, and snapshot GUIDs. It never
forces a receive, deletes files/snapshots, stops services, or switches mounts.
TrueNAS's `/mnt` pool altroot is handled by inheriting the destination mountpoint.

From the repository root, copy the script to the NAS:

```sh
scp -F /dev/null scripts/copy_immich_storage.py \
  truenas_admin@192.168.1.155:/tmp/copy_immich_storage.py
```

On the NAS, preview and then start the initial copy in a persistent terminal
session. Keep the new child dataset unexported. Receiving the full stream
creates it, preserving NFSv4 ACLs and all source files, including ZIP archives.

```sh
sudo python3 /tmp/copy_immich_storage.py seed \
  --destination main/data/photos --dry-run
sudo python3 /tmp/copy_immich_storage.py seed \
  --destination main/data/photos
```

This first copy is about 680 GiB. Verbose ZFS output reports transfer progress.
The destination stays unmounted with `canmount=noauto`; Immich remains online
on its existing storage. The script retains the source `immich-hdd-seed`
snapshot as the incremental anchor. If a transfer fails, inspect the error
before retrying; seed refuses to overwrite an existing destination. The script
also refuses source/destination child datasets, overlapping mounts, and an
encrypted source requiring a separate replication policy.

For a different destination, replace `--destination` consistently and update the
three HDD PV paths plus the readiness marker path in the Ansible preflight to
match its effective mountpoint. Do not enable the overlay with mismatched paths.

### Freeze, verify, and switch mounts

Schedule an Immich outage for the final receive, native ZFS verification, NFS
checks, and mount switch. The optional full file reread can take hours on HDDs.

On bfg, stop the server and wait until every Immich server pod has terminated:

```bash
kubectl scale deployment/immich-server --namespace immich --replicas=0
kubectl get pods --namespace immich \
  --selector=app.kubernetes.io/instance=immich,app.kubernetes.io/name=server
```

With all writers paused, create a fresh logical database backup on bfg. The
database remains running; its existing local storage is not moved.

```bash
set -euo pipefail
umask 077
immich_cutover_dir="$HOME/immich-hdd-cutover"
mkdir -p "$immich_cutover_dir"
immich_database_name="$(kubectl get secret immich-database-app --namespace immich \
  -o jsonpath='{.data.dbname}' | base64 --decode)"
kubectl exec --namespace immich immich-database-1 --container postgres -- \
  pg_dump --username=postgres --dbname="$immich_database_name" --clean --if-exists |
  gzip > "$immich_cutover_dir/database-before-hdd.sql.gz"
gzip --test "$immich_cutover_dir/database-before-hdd.sql.gz"
```

Preserve that backup separately as well. On the NAS, with all writers stopped,
run the incremental copy and native verification:

```sh
sudo python3 /tmp/copy_immich_storage.py sync \
  --destination main/data/photos --writers-stopped
sudo python3 /tmp/copy_immich_storage.py verify \
  --destination main/data/photos
```

`--writers-stopped` is the operator's confirmation, not a command to stop Immich.
The incremental receive requires the destination to remain unmounted. It creates
a uniquely named `immich-hdd-cutover-...` snapshot and uses the newest matching
copy snapshot as its incremental anchor. Both pipeline exit statuses are checked.
No forced rollback or overwriting of destination changes is performed.

Verification mounts the destination, compares snapshot GUIDs and ACL properties,
checks all six Immich marker files, and checks that the destination is unchanged
since replication. For a final sync, it also checks that the source stayed frozen.
ZFS send/receive validates stream checksums; a second full reread is optional:

```sh
sudo python3 /tmp/copy_immich_storage.py verify \
  --destination main/data/photos --full-checksum
```

That option additionally runs rsync with checksums, metadata comparison,
`--dry-run`, and `--delete`. The delete flag only reports extra destination files;
the comparison never modifies files. Keep writers stopped until it finishes.
During the actual cutover, the additional full reread was stopped to limit the
outage; it was not a completed validation. Matching final snapshot GUIDs, frozen
filesystems, ACLs, marker files, and real NFS read/write probes passed.

Check effective NFSv4 permissions on representative folders and confirm all six
Immich subdirectories retain their `.immich` markers. After successful final
verification, make the dataset mount automatically on subsequent NAS boots:

```sh
sudo zfs set canmount=on main/data/photos
```

Through the TrueNAS UI/API, add an NFS export of `/mnt/main/data/photos` for
`192.168.1.221`, matching the existing photo export's identity mapping and
access settings. Keep the existing SSD exports. On bfg, test a temporary mount
and verify reads of both external libraries and effective write permissions
for the Immich server identity in `ImmichData`; unmount the test mount afterward.

Only then, on the NAS, record readiness:

```bash
printf '%s\n' 'immich-hdd-storage-v1: frozen copy and NFS verified' |
  sudo -n tee /mnt/main/data/photos/.immich-hdd-ready >/dev/null
```

On the control machine, persist `immich_hdd_storage_enabled: true` in
`ansible/group_vars/all/vars.yml`, then run:

```sh
ansible-playbook ansible/2_services.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags immich --vault-password-file .vault_password
```

The Helm upgrade starts the server again. Before resuming client uploads, check
the pod's actual volume mounts, storage-integrity logs, existing originals and
previews, external-library assets, albums, faces, and video playback. Upload one
test asset and verify its original is on HDD and its generated previews are on
SSD. Check the GPU claims and realtime playback after the rollout.

### Import the GoPro cloud archives into an album

After the storage cutover passes validation, extract and **upload through the
Immich CLI** into one album named **GoPro Cloud**. Extraction alone does not
complete the import. Use a staging folder outside `CameraPhotos`,
`PreviousImmichUploads`, and `ImmichData`, so an external-library scan cannot
index the staging files before the managed upload.

The six `CameraPhotos/GoProDump/GoPro-*.zip` archives contain 148 MP4 videos and
6 JPEG photos (154 entries, 189.3 GiB total), with no repeated filenames. ZIP
timestamps appear to be export timestamps, so retain embedded camera metadata.
`ele30.zip` contains 770 Fujifilm RAW photos and is outside this GoPro import.

The GoPro import **completed on 2026-10-15 by 21:03 CEST**. All six archives
passed verification: 154 unique assets (148 videos and 6 photos, 189.3 GiB) are
in the `GoPro Cloud` album (`8f176147-f783-4822-92c8-93469af55ebb`). A fresh
check on 2026-10-16 confirmed every server checksum, album membership, and HDD
original size. All six extraction directories were removed after verification,
and the NAS service exited with status 0. After the authorized SSD cleanup on
2026-10-16, the ZIPs remain at
`/mnt/main/data/photos/CameraPhotos/GoProDump`; their old SSD copies were
removed. Import manifests retain the original SSD source paths as provenance.

The completed run used `scripts/import_gopro_cloud.py` on the NAS. It read the
ZIP copies on SSD, staged one archive at a time outside the external libraries,
and ran the
[official Immich CLI](https://docs.immich.app/features/command-line-interface/#usage)
in Docker. The pinned 3.1.0 image matches the server. All files go into the explicit
`GoPro Cloud` album. Keep the CLI's default client hashing and deduplication
enabled, and upload one file at a time. A previous attempt with two concurrent
large uploads and `--skip-hash` coincided with failed server health probes and
a restart; a small photo test did not reveal that failure. Extraction also
computes SHA-1, and the script checks the server checksum before cleanup.

The largest batch is 62.1 GiB. The script requires another 64 GiB of free SSD
headroom before extraction. Managed originals go to HDD through the live Immich
mounts, while thumbnails and encoded videos remain on SSD. Reserve another
189.3 GiB on HDD for uploads and allow SSD headroom for generated videos.

Install both `import_gopro_cloud.py` and `run_gopro_cloud_import.sh` in the same
directory on the NAS. Save the account API key privately with mode 600, outside
the repository. Run the shell runner as root:

```sh
sudo install -d -m 700 /mnt/fast/1tb/immich-import-staging/GoProCloud
sudo systemd-run --unit=immich-gopro-import --collect \
  -p Type=exec -p UMask=0077 \
  -p StandardOutput=append:/mnt/fast/1tb/immich-import-staging/GoProCloud/import.log \
  -p StandardError=append:/mnt/fast/1tb/immich-import-staging/GoProCloud/import.log \
  /bin/bash /tmp/run_gopro_cloud_import.sh \
  --api-key-file /run/immich-import-api-key
```

The default paths are:

- ZIPs: `/mnt/main/data/photos/CameraPhotos/GoProDump`
- Staging: `/mnt/fast/1tb/immich-import-staging/GoProCloud`
- Verification of HDD originals: `/mnt/main/data/photos`

ZIP reads validate CRCs and compute SHA-1 while extracting. The initial batch gets a CLI
dry run; a real archive photo tests the first managed upload. Later batches
reuse that validation and avoid redundant dry-run hashing. After uploading,
the script matches every input checksum to a live asset owned by the account,
checks its original exists on HDD with the expected size, and checks its album
membership. It then removes only that batch's extracted staging files. Original
ZIPs are retained on HDD. Private manifests under `.manifests` permit
resuming with the same command; failures retain staging for investigation.
The shell runner starts a fresh Python worker after extraction, each CLI call,
and verification. The import is launched as a transient systemd service, so it can continue
launching workers independently of the SSH session. Detached SSH jobs previously
failed to launch Docker with `ENOSYS` after the initiating session ended. The
SSH-launched process had a seccomp filter; the PID-1-managed service has none. Durable checkpoints
prevent repeated extraction or successful uploads. A runner lock prevents
overlapping invocations; actual errors stop with a `FAILED` line in the log.

The importer uses the LAN address for bfg while preserving the HTTPS hostname
and certificate verification. Credentials enter Docker through environment
variables, never command-line values or import logs. The import service is `immich-gopro-import.service`; its retained log is
`/mnt/fast/1tb/immich-import-staging/GoProCloud/import.log` on the NAS.

Reconcile all 154 entries against unique assets and duplicates. Byte-identical
inputs may produce fewer unique album assets. Inspect capture dates and play
representative long/chaptered videos after processing finishes.

### Rollback and reclaim space

The first real GoPro upload passed end-to-end validation: its original is on
HDD, both preview and thumbnail are on SSD, and its embedded capture date
(2022-10-28) was recovered instead of the ZIP export date. The background
comparison of the immutable migration snapshots also passed for file inventory,
sizes, timestamps, ownership, permissions, and links.

The pre-cutover SQL backup and rollback metadata are retained on bfg under
`/home/geesawra/immich-hdd-cutover-20261015/`. The backup passed gzip verification;
its SHA-256 is `aa051ecc556c57c8c5744cd82a8188a39476799d9c75e8d07f4b06ca0d339ea6`.
A fresh pre-cleanup dump is retained beside it as
`database-before-ssd-reclaim-20261016.sql.gz` (149,052,998 bytes, mode 600), with
SHA-256 `ada8df5d6a8f9c905dcd0c5bcaa7a97c4997fb5752cc562e7aa5039aba0ba319`.
It passed gzip verification.

**SSD space reclamation completed on 2026-10-16 at 10:50 CEST**, following the
owner's authorization. The retired SSD originals had no changes since the final
copy snapshot, and both matching HDD snapshot GUIDs were checked before removal.
The cleanup reclaimed **507.26 GiB**, leaving approximately **721.01 GiB
available**; the `fast` pool is now **19% used**.

Only these redundant paths under `/mnt/fast/1tb/photos` were removed:

- `CameraPhotos`
- `PreviousImmichUploads`
- `ImmichData/upload`, `ImmichData/library`, and `ImmichData/backups`
- `ele30.zip`

The live SSD `ImmichData/thumbs`, `ImmichData/encoded-video`, `ImmichData/profile`,
and their marker files are preserved, as is `ImmichMl`. All PV/PVC objects and
the SSD NFS exports remain available for the active preview mounts. The SMB
share named `photos` now points to `/mnt/main/data/photos`; Samba's
effective configuration was checked after the switch.

Both SSD migration snapshots (`immich-hdd-seed` and
`immich-hdd-cutover-20261015T163444726533Z`) were removed after live application
checks passed. Their HDD counterparts remain, along with the new snapshot
`main/data/photos@immich-before-ssd-reclaim-20261016T084847Z`. HDD originals
and ZIPs remain intact. No off-NAS media backup was verified during this work.

Checks after file removal and snapshot release confirmed healthy `fast` and
`main` pools, twelve production original reads across all three media mounts,
six Immich markers, SSD previews, HTTP thumbnail access, video range playback,
and healthy Immich pods without an additional restart. The audit is retained on
the NAS at `/mnt/main/data/immich/.maintenance/ssd-reclaim-20261016.json`.

A mount rollback now requires copying current HDD media back to SSD with
sufficient capacity before changing the storage flag or claims. Keep the current
database; the pre-cutover SQL dump does not contain subsequent uploads or edits.

Backup coverage should include HDD originals, both external libraries, ZIPs,
the SSD profile folder, and PostgreSQL. Monitor NAS `fast` and `main` plus bfg's
local scratch filesystem separately. Immich's main free-space display follows
`/data` and cannot serve as the SSD-preview capacity alert.

## NAS SSD replacement and dataset layout

On 2026-10-16 the Lexar 4 TB SSD was installed internally over native NVMe.
Its existing `torrents` pool was renamed to `fast`, and `data` was renamed to
`torrents`. The former SSD's complete `1tb` subtree was restored from verified
HDD staging before restoring the `/mnt/fast/1tb` shares.

| Dataset | Purpose and capacity policy |
|---|---|
| `fast/1tb` | Shared `quota=1T` and `reservation=1T` for photos and services (1 TiB) |
| `fast/1tb/photos` | Immich previews, encoded video, profile data, and ML-related files |
| `fast/1tb/services` | Service configuration and persistent data |
| `fast/torrents/mine` | Existing personal torrent configuration and files |
| `fast/torrents/others` | Existing shared torrent configuration and files |
| `main/data/photos` | HDD managed originals, external libraries, and archives |

The existing 3 TiB quota on `fast/torrents` is retained. The `1tb` reservation
protects its capacity from torrent growth; it also counts toward the parent
pool's reported used space before that capacity contains files.

The HDD photos dataset was renamed from `main/data/immich/photos` to
`main/data/photos` within `main`, preserving dataset/snapshot identities and
permissions. Its inherited `exec=off` value was made explicit before moving it.
The NFS and SMB share paths, Kubernetes NFS volumes, importer default, and
readiness-marker path use the new location. Immich's three static HDD PV/PVC
bindings were recreated with `Retain`, keeping their names and existing media;
container paths and PostgreSQL file paths are unchanged.

The SSD passed a 32 GiB direct write/read verification, a full scrub of its
existing 389 GiB with zero errors, and the staged restore. Restore checks
included snapshot GUIDs, dataset properties, complete service-file checksums,
and sampled photo content. Both `fast` and `main` remained healthy.

The temporary recovery tree `main/fast-staging-20261016T085900Z` was removed
after restore verification and live Immich media reads, reclaiming about
164 GiB. Before removal, 44 audit/configuration files were copied and
checksum-verified under `/mnt/main/.recovery/ssd-migration-20261016`, a private
directory rather than another dataset. Protected TrueNAS configuration exports
and audit copies are also retained off the NAS. `layout-current.json` records
current paths; older audit records retain their original path strings. The
removed SSD is not relied upon as a rollback copy.

Both Transmission deployments were restored to one replica. The shared
instance's `settings.json` contained one byte and prevented daemon startup;
its configuration was archived privately before restoring the existing
container image's default settings. Torrent and resume records were preserved;
the personal and shared instances loaded 129 and 202 torrents, respectively.

## Immich GPU layout

Immich is entirely Helm-managed. There is no production Docker ML container:

- AMD GPU DRA driver v1.0.1 publishes both GPUs to Kubernetes as individually selectable devices;
- the Immich chart runs the `v3.1.0-rocm` machine-learning component on `bfg`;
- ML requests PCI device ID `0x73a5` and receives the RX 6950 XT;
- the server requests PCI device ID `0x164e` and receives the Raphael iGPU for VAAPI transcoding and hardware decode;
- both Deployments use `maxSurge: 0`, because each DRA claim is exclusive and no spare GPU exists during a rollout;
- DRA injects `/dev/kfd` and only the selected GPU's current DRM card/render nodes through CDI, so `/dev/dri` enumeration changes do not alter the intended mapping.

AMD GPU allocation is reserved for Immich. The exclusive GPU selectors and
zero-surge rollout settings introduced in commit `36bdede` remain unchanged.

### AudioMuse removal

The services playbook uninstalls AudioMuse's Helm release, removes its old ROCm
worker and ingress, and removes the AudioMuse plugin and agent from Navidrome.
The AudioMuse PostgreSQL cluster is hibernated, not deleted: its PVCs and data
are retained, along with any old GPU cache PVCs. Navidrome's music and database
are not removed. The old AudioMuse manifests are retained only for reference;
the playbook no longer deploys them.

To apply both AudioMuse removal and Immich recovery:

```sh
ansible-playbook ansible/2_services.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags audiomuse,immich \
  --vault-password-file .vault_password
```

This changes the running services: AudioMuse stops, Navidrome rolls out to remove
its plugin, and only Immich pods with the specific missing-AMD-CDI startup error
are replaced. No PVCs, media, or database clusters are deleted.

### GPU recovery after a node reboot

AMD DRA v1.0.1 can retain prepared GPU claims while losing their claim-specific
CDI files in `/var/run/cdi` after reboot. The services playbook checks the two
Immich GPU workloads before the Helm upgrade. It replaces only ReplicaSet-owned
pods in `CreateContainerError` whose message identifies an unresolved AMD CDI
device, whose claim uses one of Immich's GPU templates, and whose generated
claim is owned by that pod. UID and resource-version preconditions protect
against replacing a pod that changed since inspection. The playbook waits for
the old claim to be garbage-collected; replacement pods obtain fresh exclusive
claims using the existing GPU selectors.

The `immich-gpu-recovery` CronJob also checks every minute, including after a
reboot once Kubernetes can schedule Jobs. It runs without GPU allocation or
host mounts and uses a namespace-scoped ServiceAccount that can read/delete
pods and read GPU claims. It checks the workload labels, GPU template, claim
ownership, and claim UID in the CDI error, then re-reads the pod and uses UID
and resource-version deletion preconditions. Healthy pods, unrelated startup
errors, named/shared claims, and terminating pods are left alone. Runs do not
overlap; each run is limited to five minutes, and it waits up to 90 seconds per
old claim for garbage collection. Changed/deleted resources are rechecked on
the next run; other failures are logged and mark the Job failed.

Deploy only the automatic recovery resources on an existing Immich installation:

```sh
ansible-playbook ansible/2_services.yml \
  --inventory ansible/inventory/hosts.yml \
  --tags immich-gpu-recovery

kubectl get cronjob,jobs -n immich
kubectl logs -n immich job/<recovery-job-name>
```

The deployment-time recovery remains available through `--tags immich`.
Recovery starts fresh claims; it does not verify ML inference or transcoding.
Use the device and health checks below after hardware or driver changes.

Do not work around this by copying old CDI entries into persistent storage:
the saved DRM paths and device numbers can change across reboots. Upstream
tracks [the missing-file failure](https://github.com/ROCm/k8s-gpu-dra-driver/issues/75)
and [safe cross-reboot recovery](https://github.com/ROCm/k8s-gpu-dra-driver/issues/86).
The recovery does not manually delete claims or alter the GPU driver checkpoint.

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
