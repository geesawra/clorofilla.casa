"""Replace only Immich pods blocked by missing AMD claim-specific CDI specs."""

import json
import logging
import os
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

NAMESPACE = "immich"
PODS = f"/api/v1/namespaces/{NAMESPACE}/pods"
CLAIMS = f"/apis/resource.k8s.io/v1/namespaces/{NAMESPACE}/resourceclaims"
TEMPLATES = {
    "server": "immich-server-raphael-gpu-v1",
    "machine-learning": "immich-ml-rx6950xt-gpu-v1",
}
CDI_ERROR = "CDI device injection failed: unresolvable CDI devices k8s.gpu.amd.com/gpu="
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LOG = logging.getLogger(__name__)


class KubernetesAPI:
    """Use the projected ServiceAccount credential with verified cluster TLS."""

    def __init__(self):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]
        self.url = f"https://{host}:{port}"
        context = ssl.create_default_context(cafile=SERVICE_ACCOUNT / "ca.crt")
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))

    def request(self, method, path, body=None):
        token = (SERVICE_ACCOUNT / "token").read_text().strip()
        request = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)


def candidate_claim(pod):
    """Return the generated claim name only for the known CDI failure."""
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    labels = metadata.get("labels", {})
    component = labels.get("app.kubernetes.io/name")
    if (
        metadata.get("namespace") != NAMESPACE
        or metadata.get("deletionTimestamp")
        or labels.get("app.kubernetes.io/instance") != "immich"
        or component not in TEMPLATES
        or spec.get("nodeName") != "bfg"
        or any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )
    ):
        return None
    owners = [o for o in metadata.get("ownerReferences", []) if o.get("controller")]
    if len(owners) != 1 or owners[0].get("kind") != "ReplicaSet":
        return None
    if not owners[0].get("name", "").startswith(f"immich-{component}-"):
        return None
    claims = [c for c in spec.get("resourceClaims", []) if c.get("name") == "gpu"]
    if (
        len(claims) != 1
        or claims[0].get("resourceClaimTemplateName") != TEMPLATES[component]
    ):
        return None
    waiting = [
        c.get("state", {}).get("waiting", {})
        for c in status.get("containerStatuses", [])
    ]
    if not any(
        c.get("reason") == "CreateContainerError" and CDI_ERROR in c.get("message", "")
        for c in waiting
    ):
        return None
    generated = [
        c for c in status.get("resourceClaimStatuses", []) if c.get("name") == "gpu"
    ]
    if len(generated) != 1 or not generated[0].get("resourceClaimName"):
        raise ValueError("Failed pod has no unique generated GPU claim")
    return generated[0]["resourceClaimName"]


def recover_pod(api, pod, claim_name):
    """Verify ownership, conditionally replace the pod, and await claim GC."""
    metadata = pod["metadata"]
    claim_path = f"{CLAIMS}/{quote(claim_name, safe='')}"
    claim = api.request("GET", claim_path)
    owners = claim["metadata"].get("ownerReferences", [])
    if not any(
        o.get("kind") == "Pod"
        and o.get("uid") == metadata["uid"]
        and o.get("name") == metadata["name"]
        and o.get("controller")
        for o in owners
    ):
        raise ValueError("GPU claim is not controlled by the failed pod")
    messages = [
        c.get("state", {}).get("waiting", {}).get("message", "")
        for c in pod["status"]["containerStatuses"]
    ]
    if not any(
        CDI_ERROR + claim["metadata"]["uid"] + "-" in message for message in messages
    ):
        raise ValueError("CDI failure does not identify this GPU claim")
    api.request(
        "DELETE",
        f"{PODS}/{quote(metadata['name'], safe='')}",
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {
                "uid": metadata["uid"],
                "resourceVersion": metadata["resourceVersion"],
            },
        },
    )
    LOG.info(
        "Replacing %s; waiting for generated claim %s to be collected",
        metadata["name"],
        claim_name,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            api.request("GET", claim_path)
        except HTTPError as error:
            if error.code != 404:
                raise
            LOG.info(
                "Recovered %s: old GPU claim collected; ReplicaSet will prepare a fresh claim",
                metadata["name"],
            )
            return
        time.sleep(2)
    raise TimeoutError(
        f"Old GPU claim {claim_name} was not collected within 90 seconds"
    )


def reconcile(api):
    """Run one bounded recovery pass; failures remain visible in Job status."""
    query = urlencode(
        {
            "labelSelector": "app.kubernetes.io/instance=immich,app.kubernetes.io/name in (server,machine-learning)"
        }
    )
    pods = api.request("GET", f"{PODS}?{query}")["items"]
    failed = False
    recovered = 0
    for snapshot in pods:
        name = snapshot["metadata"]["name"]
        try:
            if not candidate_claim(snapshot):
                continue
            # Re-read before deciding: the listed pod might already have recovered.
            pod = api.request("GET", f"{PODS}/{quote(name, safe='')}")
            claim_name = candidate_claim(pod)
            if claim_name:
                recover_pod(api, pod, claim_name)
                recovered += 1
        except HTTPError as error:
            if error.code in (404, 409):
                LOG.info(
                    "Skipped %s: resource changed during inspection (HTTP %s); next pass will recheck",
                    name,
                    error.code,
                )
            else:
                LOG.exception("Recovery failed for %s", name)
                failed = True
        except (ValueError, TimeoutError, OSError):
            LOG.exception("Recovery refused or failed for %s", name)
            failed = True
    LOG.info("Recovery check complete: %s pods replaced", recovered)
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(reconcile(KubernetesAPI()))
