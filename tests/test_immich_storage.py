"""Check opt-in activation and the storage layout rendered by the pinned chart."""

from copy import deepcopy
import os
from pathlib import Path
import subprocess

from jinja2.nativetypes import NativeEnvironment
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def test_hdd_activation_is_opt_in_and_requires_verification_before_helm():
    assert isinstance(
        load("ansible/group_vars/all/vars.yml")["immich_hdd_storage_enabled"], bool
    )
    tasks = load("ansible/2_services.yml")[1]["tasks"]
    prepare = next(
        task
        for task in tasks
        if task.get("ansible.builtin.include_tasks", {}).get("file")
        == "tasks/immich_hdd_storage.yml"
    )
    helm = next(task for task in tasks if task["name"] == "Deploy Immich through Helm")
    assert tasks.index(prepare) < tasks.index(helm)
    env = NativeEnvironment()
    env.filters["bool"] = bool
    assert env.compile_expression(prepare["when"])() is False
    for enabled in [False, True]:
        context = {"immich_hdd_storage_enabled": enabled}
        assert env.compile_expression(prepare["when"])(**context) is enabled
        files = env.from_string(helm["kubernetes.core.helm"]["values_files"]).render(
            **context
        )
        assert files == ["k8s/immich_values.yml"] + (
            ["k8s/immich_hdd_values.yml"] if enabled else []
        )
    assert env.from_string(helm["kubernetes.core.helm"]["values_files"]).render() == [
        "k8s/immich_values.yml"
    ]
    preflight = load("ansible/tasks/immich_hdd_storage.yml")
    assert "ansible.builtin.stat" in preflight[0]
    assert preflight[0]["delegate_to"] == "truenas"
    assert "ansible.builtin.assert" in preflight[1]
    assert preflight[2]["kubernetes.core.k8s"]["state"] == "present"
    assert preflight[2]["when"] == "not ansible_check_mode"


@pytest.fixture(scope="module")
def rendered_storage(tmp_path_factory):
    """Render offline when an explicit local chart archive and Helm are supplied."""
    helm = os.environ.get("IMMICH_TEST_HELM")
    chart = os.environ.get("IMMICH_TEST_CHART")
    if not helm or not chart:
        pytest.skip(
            "Set IMMICH_TEST_HELM and IMMICH_TEST_CHART to run Helm integration checks"
        )
    temporary = tmp_path_factory.mktemp("helm-storage")
    environment = os.environ | {
        f"HELM_{kind}_HOME": str(temporary / kind.lower())
        for kind in ["CACHE", "CONFIG", "DATA"]
    }
    rendered = []
    for split in [False, True]:
        command = [
            helm,
            "template",
            "immich",
            chart,
            "--namespace",
            "immich",
            "--kube-version",
            "1.35.0",
            "--skip-schema-validation",
            "--values",
            str(ROOT / "ansible/k8s/immich_values.yml"),
        ]
        if split:
            command += ["--values", str(ROOT / "ansible/k8s/immich_hdd_values.yml")]
        output = subprocess.run(
            command, check=True, capture_output=True, text=True, env=environment
        ).stdout
        rendered.append(
            {
                document["metadata"]["name"]: document
                for document in yaml.safe_load_all(output)
                if document and document["kind"] == "Deployment"
            }
        )
    resources = {}
    for filename in ["immich.yml", "immich_hdd_storage.yml"]:
        for document in yaml.safe_load_all(
            (ROOT / "ansible/k8s" / filename).read_text()
        ):
            if document and document["kind"] in {
                "PersistentVolume",
                "PersistentVolumeClaim",
            }:
                key = document["kind"], document["metadata"]["name"]
                assert (
                    key not in resources
                ), "The split must not replace existing volume resources"
                resources[key] = document
    return rendered, resources


def resolve_path(deployment, resources, path):
    """Resolve nested mounts to the NAS path or ephemeral backing store."""
    pod = deployment["spec"]["template"]["spec"]
    mounts = pod["containers"][0]["volumeMounts"]
    mount = max(
        (
            item
            for item in mounts
            if path == item["mountPath"] or path.startswith(item["mountPath"] + "/")
        ),
        key=lambda item: len(item["mountPath"]),
    )
    volume = next(item for item in pod["volumes"] if item["name"] == mount["name"])
    if "emptyDir" in volume:
        assert "medium" not in volume["emptyDir"], "Scratch must use the node disk"
        return "node-disk", mount
    claim = resources[
        "PersistentVolumeClaim", volume["persistentVolumeClaim"]["claimName"]
    ]
    pv = resources["PersistentVolume", claim["spec"]["volumeName"]]
    suffix = path.removeprefix(mount["mountPath"]).lstrip("/")
    physical = Path(pv["spec"]["nfs"]["path"]) / mount.get("subPath", "") / suffix
    return str(physical), mount


def test_rendered_originals_move_but_previews_and_external_paths_remain_valid(
    rendered_storage,
):
    (baseline, split), resources = rendered_storage
    server = split["immich-server"]
    for path, expected in [
        (
            "/data/upload/photo.jpg",
            "/mnt/main/data/photos/ImmichData/upload/photo.jpg",
        ),
        (
            "/data/library/photo.jpg",
            "/mnt/main/data/photos/ImmichData/library/photo.jpg",
        ),
        (
            "/data/backups/db.sql.gz",
            "/mnt/main/data/photos/ImmichData/backups/db.sql.gz",
        ),
        ("/external/photo.jpg", "/mnt/main/data/photos/CameraPhotos/photo.jpg"),
        (
            "/existing-uploads/photo.jpg",
            "/mnt/main/data/photos/PreviousImmichUploads/photo.jpg",
        ),
    ]:
        physical, mount = resolve_path(server, resources, path)
        assert physical == expected
        if path.startswith(("/external/", "/existing-uploads/")):
            assert mount["readOnly"] is True
    for folder in ["thumbs", "encoded-video", "profile"]:
        path = f"/data/{folder}/sample"
        previous, _ = resolve_path(baseline["immich-server"], resources, path)
        current, _ = resolve_path(server, resources, path)
        assert previous == current == f"/mnt/fast/1tb/photos/ImmichData/{folder}/sample"
    assert resolve_path(server, resources, "/tmp/scratch")[0] == "node-disk"
    for (kind, name), resource in resources.items():
        if kind == "PersistentVolume" and name.endswith("-hdd"):
            assert resource["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
            claim_ref = resource["spec"]["claimRef"]
            claim = resources["PersistentVolumeClaim", claim_ref["name"]]
            assert claim_ref["namespace"] == claim["metadata"]["namespace"] == "immich"
            assert claim["spec"]["volumeName"] == name


def test_storage_overlay_preserves_gpu_database_and_model_cache_configuration(
    rendered_storage,
):
    (baseline, split), _ = rendered_storage
    assert baseline["immich-machine-learning"] == split["immich-machine-learning"]
    previous = deepcopy(baseline["immich-server"]["spec"]["template"]["spec"])
    current = deepcopy(split["immich-server"]["spec"]["template"]["spec"])
    for pod in [previous, current]:
        del pod["volumes"]
        for container in pod["containers"]:
            del container["volumeMounts"]
    assert previous == current
