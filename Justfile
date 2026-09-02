set shell := ["bash", "-euo", "pipefail", "-c"]

# Install every Ansible collection and role required by the playbooks.
# Galaxy is only contacted when one of the declared dependencies is missing.
bootstrap:
    if ! python3 -c 'import kubernetes' 2>/dev/null; then python3 -m pip install --user kubernetes; fi
    if ! for collection in kubernetes.core artis3n.tailscale k3s.orchestration ansible.posix community.general; do ansible-galaxy collection list "$collection" 2>/dev/null | grep -Fq "$collection" || exit 1; done; then ansible-galaxy collection install -r ansible/collections/requirements.yml; fi
    if ! ansible-galaxy role list 2>/dev/null | grep -Fq 'exploide.dnf-automatic'; then ansible-galaxy role install -r ansible/roles/requirements.yml; fi

# Validate YAML and Ansible syntax after ensuring dependencies are installed.
check: bootstrap
    ansible-playbook --syntax-check ansible/1_setup.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
    ansible-playbook --syntax-check ansible/2_services.yml -i ansible/inventory/hosts.yml
    ansible-playbook --syntax-check ansible/3_monitoring.yml -i ansible/inventory/hosts.yml

# Provision bfg, deploy services, then deploy monitoring.
deploy: bootstrap
    TAILSCALE_KEY="$TAILSCALE_KEY" ansible-playbook ansible/1_setup.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
    ansible-playbook ansible/2_services.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
    ansible-playbook ansible/3_monitoring.yml -i ansible/inventory/hosts.yml --vault-password-file .vault_password
