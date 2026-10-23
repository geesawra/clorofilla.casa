# clorofilla.casa

Personal Ansible and K3s configuration for one Fedora host, `bfg`. It assumes
my home network and is not a general-purpose installer. Routes are intended
for private network access.

## Requirements

The control machine needs Ansible, `just`, `kubectl`, Helm (`/usr/bin/helm3`),
Git, and Python 3. The target host must be reachable over SSH with sudo.

Create `.vault_password` locally and provide `TAILSCALE_KEY` for initial host
setup. Do not commit either secret. Bootstrap dependencies and check the
playbooks with:

```sh
just bootstrap
just check
```

## Deploy

`just deploy` runs host setup, services, and monitoring in order:

```sh
TAILSCALE_KEY='tskey-…' just deploy
```

To apply one service or playbook, use its Ansible tag and the playbook under
`ansible/`.

## Manual service setup

- **Miniflux**: available at
  [`miniflux.clorofilla.casa`](https://miniflux.clorofilla.casa).
  Deploy with the `miniflux` tag. On a fresh database, create an administrator
  with `kubectl exec -it -n miniflux deploy/miniflux -- /usr/bin/miniflux -create-admin`;
  use `-reset-password` to recover access. After creating `admin` on a fresh database,
  rerun the `miniflux` tag. Deployment enables **Google Reader API** for `admin`,
  initially using its login credentials and preserving any separately configured
  integration credentials on later runs. Change those under **Settings → Integrations**.
  Use `https://miniflux.clorofilla.casa` as the client server URL.
  PostgreSQL data stays on local storage
  on `bfg`. See [`miniflux.yml`](ansible/k8s/miniflux.yml).
- **Gonic**: the music endpoint remains
  [`navidrome.clorofilla.casa`](https://navidrome.clorofilla.casa). Its web page
  manages accounts and scans; use a Subsonic client for playback. A fresh
  database starts with `admin` / `admin`; change that password before use.
  Accounts and playlists are separate from Navidrome; its old data PVC
  remains in namespace `navidrome` for recovery.
  Gonic's database, playlists, and podcasts use local storage on `bfg`;
  music stays read-only on the NAS. See [`gonic.yml`](ansible/k8s/gonic.yml).
- **Audiobookshelf**: create the initial administrator at
  [`ab.clorofilla.casa`](https://ab.clorofilla.casa). The current config already
  has a `Mouse` library at `/audiobooks`, mounted read-only from the mouse
  directory. A new config PVC needs the library added in the app.
- **Lidarr**: configure the Redacted indexer and download clients in the web UI.
- **Jackett**: for indexers that need Cloudflare solving, set the FlareSolverr
  API URL to `http://flaresolverr-service.torrents.svc.cluster.local:8191`.
- **Torrent search (`ts`)**: provide its private configuration as the
  `ts-config` Secret; keep its hostname on the trusted network because browser
  Basic Authentication is disabled. The TOML should use the in-cluster URLs
  `http://jackett-service:9117`,
  `http://transmission-service:9091/transmission/rpc`, and
  `http://transmission-others-service:9091/transmission/rpc`.

Create or update the `ts-config` Secret from a private TOML file with:

```sh
kubectl create secret generic ts-config \
  --namespace torrents \
  --from-file=config.toml=/secure/path/ts-config.toml \
  --dry-run=client -o yaml | kubectl apply -f -
```

## State and recovery

Applying the playbooks does not back up or migrate application data. Preserve
PVCs, databases, and media separately before a cluster rebuild or storage
change. The Immich storage and import utilities are in [`scripts/`](scripts/);
review their options and prerequisites before running them.

## Repository map

- `ansible/1_setup.yml`: host and K3s setup
- `ansible/2_services.yml`: application workloads
- `ansible/3_monitoring.yml`: monitoring stack
- `ansible/k8s/`: Kubernetes manifests and Helm values
- `ansible/group_vars/all/`: shared variables and encrypted secrets
