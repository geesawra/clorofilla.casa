# clorofilla.casa IaC

This is my first attempt at writing IaC """code""" for my home server.

It's a very basic Ansible-based setup:
- OS is AlmaLinux 10 for maximum street cred and stability
- service orchestration is managed by K3s
- storage is handled by my NAS
- everything is only accessible either through local IPs or Tailscale
- TLS managed by LetsEncrypt
- DNS handled by PorkBun, using Tailscale IPs
- all secrets are encrypted with Ansible Vault

Playbooks will take care of enrolling the system in the Tailscale Tailnet 
attached to the provided API key.

## Prerequisites

Install required Ansible Galaxy roles and collections:

```sh
cd ansible
ansible-galaxy collection install -r collections/requirements.yml
ansible-galaxy collection install git+https://github.com/k3s-io/k3s-ansible.git
ansible-galaxy collection install artis3n.tailscale
ansible-galaxy role install exploide.dnf-automatic
```

You also need:
- Ansible (tested with 2.14+)
- `kubectl` — [install guide](https://kubernetes.io/docs/tasks/tools/)
- `helm` / `helm3` — [install guide](https://helm.sh/docs/intro/install/)
- Python `kubernetes` library: `pip install kubernetes`
- Ansible Vault password file at `.vault_password` (or create one)
- A Tailscale auth key (set as `TAILSCALE_KEY` env var)

## How do I deploy this thing

Suppose you have a freshly-built system (AlmaLinux 10 for the server, Debian for agents),
for which you have SSH keys configured, run playbooks in order:

```sh
# 1 — System init, Tailscale, K3s, kernel pinning
TAILSCALE_KEY='tskey-your-API-key-here' ansible-playbook ansible/1_setup.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password

# 2 — Services (K8s workloads)
ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password

# 3 — Monitoring (Docker, node_exporter, Prometheus agent)
ansible-playbook ansible/3_monitoring.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
```

After `1_setup.yml` completes, your kubeconfig is at `~/.kube/config`:

```sh
kubectl get nodes
```

### MNT Reform NPU (optional)

If you have a reform node with the DSI display variant, install the RKNPU kernel module:

```sh
ansible-playbook ansible/rknpu-reform.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
# Reboot the reform after this to activate the new device tree
```

## A note on reproducibility

This repo is mostly to keep me safe, you're not really supposed to deploy this stuff anywhere else, though feel free to draw inspiration from it!
