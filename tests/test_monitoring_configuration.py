"""Validate monitoring manifests without contacting the cluster."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def documents():
    return list(yaml.safe_load_all((ROOT / "ansible/k8s/monitoring/prometheus.yml").read_text()))


def test_kubelet_scrapes_verify_tls_and_authenticate():
    config = next(doc for doc in documents() if doc["kind"] == "ConfigMap")
    jobs = yaml.safe_load(config["data"]["prometheus.yml"])["scrape_configs"]
    kubelet = [job for job in jobs if job.get("scheme") == "https"]
    assert {job["metrics_path"] for job in kubelet} == {"/metrics", "/metrics/cadvisor"}
    for job in kubelet:
        assert job["authorization"]["credentials_file"].endswith("/serviceaccount/token")
        assert job["tls_config"]["ca_file"].endswith("/serviceaccount/ca.crt")
        assert not job["tls_config"].get("insecure_skip_verify")
        assert job["static_configs"][0]["targets"] == ["bfg:10250"]
    cadvisor = next(job for job in kubelet if job["metrics_path"] == "/metrics/cadvisor")
    assert "metric_relabel_configs" not in cadvisor
    assert cadvisor["static_configs"][0]["labels"]["metrics_path"] == "/metrics/cadvisor"


def test_prometheus_permissions_are_metrics_only_for_bfg():
    docs = documents()
    role = next(doc for doc in docs if doc["kind"] == "ClusterRole")
    assert role["rules"] == [{
        "apiGroups": [""], "resources": ["nodes/metrics"],
        "resourceNames": ["bfg"], "verbs": ["get"],
    }]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "prometheus"
    assert deployment["spec"]["strategy"] == {"type": "Recreate", "rollingUpdate": None}
    binding = next(doc for doc in docs if doc["kind"] == "ClusterRoleBinding")
    assert binding["subjects"] == [{
        "kind": "ServiceAccount", "name": "prometheus", "namespace": "monitoring",
    }]


def test_monitoring_rollouts_use_configuration_checksums():
    plays = yaml.safe_load((ROOT / "ansible/3_monitoring.yml").read_text())
    tasks = plays[-1]["tasks"]
    rollouts = [task for task in tasks if task["name"].startswith("Roll out")]
    assert len(rollouts) == 2
    for task in rollouts:
        annotations = task["kubernetes.core.k8s"]["definition"]["spec"]["template"]["metadata"]["annotations"]
        assert "sha256" in annotations["checksum/config"]
        assert task["when"] == "not ansible_check_mode"
    script = next(task for task in tasks if task["name"].startswith("Provision the Grafana"))
    assert script["kubernetes.core.k8s"]["definition"]["metadata"]["name"] == "grafana-dashboard-preparer"
