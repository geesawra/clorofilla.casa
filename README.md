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

## How do I deploy this thing

Suppose you have a freshly-built AlmaLinux 10 system, for which you have SSH keys configured,
you should run playbooks in the order they appear given their filename:

```sh
TAILSCALE_KEY='tskey-your-API-key-here' ansible-playbook ansible/setup.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
```

After that's done, the system from which you're executing the playbooks should have a Kubeconfig file in `~/.kube/config`: you're ready to use K8s:

```sh
ansible-playbook services.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
```

## A note on reproducibility

This repo is mostly to keep me safe, you're not really supposed to deploy this stuff anywhere else, though feel free to draw inspiration from it!
