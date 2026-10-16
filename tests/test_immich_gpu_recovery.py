"""Exercise automatic recovery decisions and API races without disrupting Immich."""

from copy import deepcopy
import importlib.util
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recovery", ROOT / "ansible/k8s/immich_gpu_recovery.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def failed_pod(component="server"):
    return {
        "metadata": {
            "name": f"immich-{component}-abc-123",
            "namespace": "immich",
            "uid": "pod-uid",
            "resourceVersion": "123",
            "labels": {
                "app.kubernetes.io/instance": "immich",
                "app.kubernetes.io/name": component,
            },
            "ownerReferences": [
                {
                    "kind": "ReplicaSet",
                    "controller": True,
                    "name": f"immich-{component}-abc",
                }
            ],
        },
        "spec": {
            "nodeName": "bfg",
            "resourceClaims": [
                {
                    "name": "gpu",
                    "resourceClaimTemplateName": recovery.TEMPLATES[component],
                }
            ],
        },
        "status": {
            "containerStatuses": [
                {
                    "state": {
                        "waiting": {
                            "reason": "CreateContainerError",
                            "message": "failed to create containerd container: "
                            + recovery.CDI_ERROR
                            + "claim-uid-gpu-0-129",
                        }
                    }
                }
            ],
            "resourceClaimStatuses": [
                {"name": "gpu", "resourceClaimName": "generated-claim"}
            ],
        },
    }


class FakeAPI:
    def __init__(self, pod):
        self.snapshot = deepcopy(pod)
        self.pod = pod
        self.deleted = []
        self.claim = {
            "metadata": {
                "uid": "claim-uid",
                "ownerReferences": [
                    {
                        "kind": "Pod",
                        "controller": True,
                        "name": pod["metadata"]["name"],
                        "uid": "pod-uid",
                    }
                ],
            }
        }
        self.conflict = False
        self.collect_claim = True

    def request(self, method, path, body=None):
        if path.startswith(recovery.PODS + "?"):
            return {"items": [self.snapshot]}
        if path.startswith(recovery.CLAIMS + "/"):
            assert method == "GET"
            if self.deleted and self.collect_claim:
                raise HTTPError(path, 404, "Not Found", {}, None)
            return self.claim
        assert path == recovery.PODS + "/" + self.pod["metadata"]["name"]
        if method == "GET":
            return self.pod
        assert method == "DELETE"
        if self.conflict:
            raise HTTPError(path, 409, "Conflict", {}, None)
        self.deleted.append((path, body))
        return {}


@pytest.mark.parametrize("component", ["server", "machine-learning"])
def test_failed_pod_gets_fresh_claim_through_conditional_pod_deletion(component):
    api = FakeAPI(failed_pod(component))
    assert recovery.reconcile(api) == 0
    assert len(api.deleted) == 1
    assert api.deleted[0][1]["preconditions"] == {
        "uid": "pod-uid",
        "resourceVersion": "123",
    }


@pytest.mark.parametrize(
    "case",
    [
        "healthy",
        "wrong_namespace",
        "other_app",
        "wrong_component",
        "wrong_node",
        "terminating",
        "unmanaged",
        "other_replicaset",
        "named_claim",
        "wrong_template",
        "other_error",
        "image_pull",
    ],
)
def test_unrelated_or_healthy_pods_are_never_deleted(case):
    pod = failed_pod()
    if case == "healthy":
        pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    elif case == "wrong_namespace":
        pod["metadata"]["namespace"] = "other"
    elif case == "other_app":
        pod["metadata"]["labels"]["app.kubernetes.io/instance"] = "other"
    elif case == "wrong_component":
        pod["metadata"]["labels"]["app.kubernetes.io/name"] = "valkey"
    elif case == "wrong_node":
        pod["spec"]["nodeName"] = "other"
    elif case == "terminating":
        pod["metadata"]["deletionTimestamp"] = "2026-10-15T15:00:00Z"
    elif case == "unmanaged":
        pod["metadata"]["ownerReferences"] = []
    elif case == "other_replicaset":
        pod["metadata"]["ownerReferences"][0]["name"] = "unrelated"
    elif case == "named_claim":
        pod["spec"]["resourceClaims"] = [{"name": "gpu", "resourceClaimName": "shared"}]
    elif case == "wrong_template":
        pod["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] = (
            recovery.TEMPLATES["machine-learning"]
        )
    elif case == "other_error":
        pod["status"]["containerStatuses"][0]["state"]["waiting"][
            "message"
        ] = "unrelated startup error"
    elif case == "image_pull":
        pod["status"]["containerStatuses"][0]["state"]["waiting"][
            "reason"
        ] = "ImagePullBackOff"
    api = FakeAPI(pod)
    assert recovery.reconcile(api) == 0
    assert not api.deleted


@pytest.mark.parametrize("case", ["missing_claim", "wrong_owner", "wrong_claim_uid"])
def test_ambiguous_or_unrelated_claims_fail_without_deleting_pods(case):
    api = FakeAPI(failed_pod())
    if case == "missing_claim":
        api.snapshot["status"]["resourceClaimStatuses"] = []
    elif case == "wrong_owner":
        api.claim["metadata"]["ownerReferences"][0]["uid"] = "other-pod"
    elif case == "wrong_claim_uid":
        api.claim["metadata"]["uid"] = "other-claim"
    assert recovery.reconcile(api) == 1
    assert not api.deleted


def test_pod_that_recovers_after_listing_is_left_alone():
    api = FakeAPI(failed_pod())
    api.pod["status"]["containerStatuses"][0]["state"] = {"running": {}}
    assert recovery.reconcile(api) == 0
    assert not api.deleted


def test_concurrent_change_is_rechecked_next_run_without_unconditional_delete():
    api = FakeAPI(failed_pod())
    api.conflict = True
    assert recovery.reconcile(api) == 0
    assert not api.deleted


def test_gc_timeout_fails_the_job_without_deleting_the_claim(monkeypatch):
    api = FakeAPI(failed_pod())
    api.collect_claim = False
    monkeypatch.setattr(recovery.time, "monotonic", Mock(side_effect=[0, 0, 91]))
    monkeypatch.setattr(recovery.time, "sleep", Mock())
    assert recovery.reconcile(api) == 1
    assert len(api.deleted) == 1


def test_automatic_job_is_bounded_and_has_only_namespaced_recovery_permissions():
    docs = list(
        yaml.safe_load_all((ROOT / "ansible/k8s/immich_gpu_recovery.yml").read_text())
    )
    assert {d["metadata"]["namespace"] for d in docs} == {"immich"}
    role = next(d for d in docs if d["kind"] == "Role")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "delete"]},
        {
            "apiGroups": ["resource.k8s.io"],
            "resources": ["resourceclaims"],
            "verbs": ["get"],
        },
    ]
    cron = next(d for d in docs if d["kind"] == "CronJob")["spec"]
    assert cron["schedule"] == "* * * * *"
    assert cron["concurrencyPolicy"] == "Forbid"
    assert cron["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 300
    pod = cron["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "immich-gpu-recovery"
    assert not pod.get("hostNetwork") and not pod.get("hostPID")
    assert all("hostPath" not in volume for volume in pod["volumes"])
    assert all(
        "claims" not in container.get("resources", {})
        for container in pod["containers"]
    )


def test_services_playbook_installs_script_and_cronjob_with_selective_tag():
    tasks = yaml.safe_load((ROOT / "ansible/2_services.yml").read_text())[1]["tasks"]
    recovery_tasks = [t for t in tasks if "immich-gpu-recovery" in t.get("tags", "")]
    assert len(recovery_tasks) == 2
    assert (
        "immich_gpu_recovery.py"
        in recovery_tasks[0]["kubernetes.core.k8s"]["definition"]["data"]
    )
    assert (
        recovery_tasks[1]["kubernetes.core.k8s"]["src"] == "k8s/immich_gpu_recovery.yml"
    )
    assert all(t["when"] == "not ansible_check_mode" for t in recovery_tasks)
