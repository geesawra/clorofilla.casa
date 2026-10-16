"""Check service teardown and GPU recovery without contacting Kubernetes."""

from copy import deepcopy
from pathlib import Path
import re

from jinja2 import Environment
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def recovery_candidate():
    return {
        "metadata": {"ownerReferences": [{"kind": "ReplicaSet"}]},
        "spec": {
            "resourceClaims": [
                {
                    "name": "gpu",
                    "resourceClaimTemplateName": "immich-ml-rx6950xt-gpu-v1",
                }
            ]
        },
        "status": {
            "containerStatuses": [
                {
                    "state": {
                        "waiting": {
                            "reason": "CreateContainerError",
                            "message": (
                                "CDI device injection failed: unresolvable CDI devices "
                                "k8s.gpu.amd.com/gpu=claim-uid-gpu-1-128"
                            ),
                        }
                    }
                }
            ]
        },
    }


def should_recover(pod):
    env = Environment()
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    task = load("ansible/tasks/immich_gpu_recovery.yml")[1]
    return all(
        env.compile_expression(condition)(immich_gpu_pod=pod)
        for condition in task["when"]
    )


def test_only_known_template_backed_missing_cdi_pods_are_recovered():
    assert should_recover(recovery_candidate())
    server = recovery_candidate()
    server["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] = (
        "immich-server-raphael-gpu-v1"
    )
    assert should_recover(server)

    candidates = []
    healthy = recovery_candidate()
    healthy["status"]["containerStatuses"][0]["state"] = {"running": {}}
    candidates.append(healthy)
    for field, value in [
        ("reason", "ImagePullBackOff"),
        ("message", "unrelated container startup error"),
    ]:
        pod = recovery_candidate()
        pod["status"]["containerStatuses"][0]["state"]["waiting"][field] = value
        candidates.append(pod)
    named_claim = recovery_candidate()
    named_claim["spec"]["resourceClaims"] = [
        {"name": "gpu", "resourceClaimName": "shared-claim"}
    ]
    candidates.append(named_claim)
    other_template = deepcopy(server)
    other_template["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] = "other"
    candidates.append(other_template)
    candidates.append({"metadata": {}, "spec": {}, "status": {}})
    for pod in candidates:
        assert not should_recover(pod)
    unmanaged = recovery_candidate()
    unmanaged["metadata"]["ownerReferences"] = []
    assert not should_recover(unmanaged)


def test_recovery_deletes_only_pods_with_preconditions_and_waits_for_claim_gc():
    tasks = load("ansible/tasks/immich_gpu_recover_pod.yml")
    deletion = next(
        task["kubernetes.core.k8s"]
        for task in tasks
        if task.get("kubernetes.core.k8s", {}).get("state") == "absent"
    )
    assert deletion["kind"] == "Pod"
    assert deletion["namespace"] == "immich"
    assert set(deletion["delete_options"]["preconditions"]) == {
        "uid", "resourceVersion"
    }
    assertions = [task["ansible.builtin.assert"] for task in tasks if "ansible.builtin.assert" in task]
    assert len(assertions) == 2
    assert "resourceClaimName" in assertions[0]["that"][1]
    assert "ownerReferences" in assertions[1]["that"][1]
    assert "kubernetes.core.k8s_info" in tasks[-1]
    assert "length == 0" in tasks[-1]["until"]


def test_audiomuse_teardown_retains_database_and_storage():
    tasks = load("ansible/2_services.yml")[1]["tasks"]
    uninstall = next(task for task in tasks if task["name"] == "Uninstall AudioMuse-AI")
    assert uninstall["kubernetes.core.helm"]["release_state"] == "absent"
    hibernate = next(task for task in tasks if task["name"].startswith("Hibernate"))
    assert hibernate["kubernetes.core.k8s"]["definition"]["metadata"]["annotations"] == {
        "cnpg.io/hibernation": "on"
    }
    for task in tasks:
        resource = task.get("kubernetes.core.k8s", {})
        if resource.get("state") == "absent":
            assert resource.get("kind") not in {"PersistentVolumeClaim", "Cluster"}
    assert not any(
        task.get("kubernetes.core.k8s", {}).get("src", "").startswith("k8s/audiomuse")
        for task in tasks
    )


def test_exclusive_immich_gpu_configuration_is_preserved():
    values = load("ansible/k8s/immich_values.yml")
    for component in ["server", "machine-learning"]:
        controller = values[component]["controllers"]["main"]
        assert controller["rollingUpdate"]["surge"] == "0"
        assert controller["containers"]["main"]["resources"]["claims"] == [
            {"name": "gpu"}
        ]
    documents = yaml.safe_load_all((ROOT / "ansible/k8s/immich.yml").read_text())
    claims = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc and doc["kind"] == "ResourceClaimTemplate"
    }
    for name, device_id in [
        ("immich-server-raphael-gpu-v1", "0x164e"),
        ("immich-ml-rx6950xt-gpu-v1", "0x73a5"),
    ]:
        request = claims[name]["spec"]["spec"]["devices"]["requests"][0]["exactly"]
        assert request["allocationMode"] == "ExactCount"
        assert request["count"] == 1
        assert device_id in request["selectors"][0]["cel"]["expression"]


def test_immich_readiness_is_polled_after_recovery():
    tasks = load("ansible/2_services.yml")[1]["tasks"]
    for register in ["immich_ml_pods", "immich_server_pods"]:
        task = next(item for item in tasks if item.get("register") == register)
        assert task["retries"] == 120
        assert task["delay"] == 5
        assert "length == 1" in task["until"][0]
        assert "Ready" in task["until"][1]


def test_tuned_is_optional_and_selects_only_the_requested_profile():
    plays = load("ansible/1_setup.yml")
    play = next(item for item in plays if item.get("tags") == "tuned")
    assert play["hosts"] == "bfg"
    assert play["become"] is True
    assert play["tasks"][0]["ansible.builtin.stat"]["path"] == "/usr/sbin/tuned-adm"
    block = play["tasks"][1]
    assert "tuned_adm.stat.exists" in block["when"]
    selection = next(task for task in block["block"] if task["name"].startswith("Select"))
    assert selection["ansible.builtin.command"]["argv"] == [
        "/usr/sbin/tuned-adm", "profile", "balanced-battery"
    ]
    env = Environment()
    condition = selection["when"][1]
    expression = env.compile_expression(condition)
    assert not expression(tuned_active={"stdout": "Current active profile: balanced-battery\n"})
    assert expression(tuned_active={"stdout": "Current active profile: performance\n"})
    assert expression(tuned_active={"stdout": "No current active profile.\n"})
    availability = next(task for task in block["block"] if task["name"].startswith("Verify the balanced"))
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    check = env.compile_expression(availability["ansible.builtin.assert"]["that"][0])
    assert check(tuned_profiles={"stdout": "Available profiles:\n- balanced-battery - Balanced profile\n"})
    assert check(tuned_profiles={"stdout": "Available profiles:\n- balanced-battery\n"})
    assert not check(tuned_profiles={"stdout": "Available profiles:\n- balanced\n"})
    assert all("not ansible_check_mode" in task["when"] for task in block["block"])


def test_navidrome_no_longer_installs_or_uses_audiomuse():
    text = (ROOT / "ansible/k8s/navidrome.yml").read_text()
    assert "wget" not in text
    assert "rm -f /data/plugins/audiomuseai.ndp" in text
    documents = list(yaml.safe_load_all(text))
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    agents = next(item["value"] for item in env if item["name"] == "ND_AGENTS")
    assert agents == "lastfm,spotify,deezer"


def test_all_traefik_routes_are_discoverable_by_homepage():
    host_pattern = re.compile(r"Host\(`([^`]+)`\)")
    route_count = 0

    for path in (ROOT / "ansible/k8s").rglob("*.yml"):
        for document in yaml.safe_load_all(path.read_text()):
            if not document or document.get("kind") != "IngressRoute":
                continue
            route_count += 1
            annotations = document["metadata"].get("annotations", {})
            assert annotations.get("gethomepage.dev/enabled") == "true", path
            assert annotations.get("gethomepage.dev/name"), path
            assert annotations.get("gethomepage.dev/group"), path
            href = annotations.get("gethomepage.dev/href", "")
            assert href.startswith("https://"), path
            assert href.removeprefix("https://") in {
                host
                for route in document["spec"]["routes"]
                for host in host_pattern.findall(route.get("match", ""))
            }, path

    assert route_count > 0


def test_homepage_uses_cluster_traefik_discovery_with_read_only_rbac():
    manifest = list(yaml.safe_load_all((ROOT / "ansible/k8s/homepage.yml").read_text()))
    config_map = next(document for document in manifest if document["kind"] == "ConfigMap")
    kubernetes_config = yaml.safe_load(config_map["data"]["kubernetes.yaml"])
    assert kubernetes_config == {
        "mode": "cluster",
        "ingress": False,
        "traefik": True,
    }

    cluster_role = next(document for document in manifest if document["kind"] == "ClusterRole")
    assert cluster_role["rules"] == [
        {
            "apiGroups": ["traefik.io"],
            "resources": ["ingressroutes"],
            "verbs": ["get", "list"],
        }
    ]

    deployment = next(document for document in manifest if document["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/gethomepage/homepage:v2.5.0"
    assert container["env"][1]["value"] == (
        "$(MY_POD_IP):3000,home.clorofilla.casa"
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    init_container = pod_spec["initContainers"][0]
    assert init_container["image"] == container["image"]
    assert init_container["args"] == [
        'for file in /config-source/*; do cp -L "$file" /app/config/; done'
    ]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["homepage-config"]["emptyDir"] == {}
    assert volumes["homepage-config-source"]["configMap"]["name"] == "homepage"
    assert container["volumeMounts"] == [
        {"name": "homepage-config", "mountPath": "/app/config"}
    ]

    tasks = load("ansible/2_services.yml")[1]["tasks"]
    deploy = next(task for task in tasks if task["name"] == "Deploy Homepage service catalog")
    assert deploy["kubernetes.core.k8s"]["src"] == "k8s/homepage.yml"
    assert "homepage" in deploy["tags"]
    assert deploy["when"] == "not ansible_check_mode"
    rollout = next(
        task
        for task in tasks
        if task["name"] == "Roll out Homepage when its configuration changes"
    )
    assert rollout["when"] == "not ansible_check_mode"
