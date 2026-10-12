"""Regression tests for offline Grafana dashboard preparation."""

from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ansible/k8s/monitoring/prepare_dashboards.py"
SPEC = importlib.util.spec_from_file_location("prepare_dashboards", SCRIPT)
assert SPEC and SPEC.loader
prepare_dashboards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_dashboards)


class GrafanaDashboardTests(unittest.TestCase):
    """Check dashboard normalization, downloads, and manifest integration."""

    def test_normalizes_static_prometheus_references_without_changing_dashboard_id(self):
        dashboard = {
            "id": 13332,
            "uid": "dashboard-uid",
            "__inputs": [
                {
                    "name": "CUSTOM_PROM",
                    "type": "datasource",
                    "pluginId": "prometheus",
                },
                {"name": "LOKI", "type": "datasource", "pluginId": "loki"},
            ],
            "panels": [
                {"datasource": "${DS_PROMETHEUS}"},
                {"datasource": "${CUSTOM_PROM}"},
                {"datasource": "Prometheus"},
                {"datasource": {"type": "prometheus", "uid": "old-prometheus"}},
            ],
        }

        result = prepare_dashboards.normalize_dashboard(deepcopy(dashboard))

        self.assertEqual(result["id"], 13332)
        self.assertEqual(result["uid"], "dashboard-uid")
        self.assertEqual(
            [panel["datasource"] for panel in result["panels"]],
            [
                "prometheus",
                "prometheus",
                "prometheus",
                {"type": "prometheus", "uid": "prometheus"},
            ],
        )

    def test_preserves_dynamic_prometheus_variables_and_sets_their_default(self):
        dashboard = {
            "templating": {
                "list": [
                    {
                        "name": "ds_prometheus",
                        "type": "datasource",
                        "query": "prometheus",
                        "current": {"text": "legacy", "value": "legacy"},
                    },
                    {
                        "name": "prometheus_dict",
                        "type": "datasource",
                        "query": {"pluginId": "prometheus"},
                    },
                    {
                        "name": "region",
                        "type": "query",
                        "query": "label_values(region)",
                    },
                ]
            },
            "panels": [
                {"datasource": {"type": "prometheus", "uid": "${ds_prometheus}"}},
                {"datasource": "$ds_prometheus"},
                {"datasource": {"type": "prometheus", "uid": "$prometheus_dict"}},
                {"datasource": "$region"},
            ],
        }

        result = prepare_dashboards.normalize_dashboard(deepcopy(dashboard))

        expected_current = {
            "selected": True,
            "text": "Prometheus",
            "value": "prometheus",
        }
        self.assertEqual(result["templating"]["list"][0]["current"], expected_current)
        self.assertEqual(result["templating"]["list"][1]["current"], expected_current)
        self.assertEqual(result["panels"], dashboard["panels"])

    def test_preserves_grafana_and_non_prometheus_datasources(self):
        dashboard = {
            "annotations": {
                "list": [
                    {"datasource": "-- Grafana --"},
                    {"datasource": {"type": "datasource", "uid": "grafana"}},
                ]
            },
            "panels": [
                {"datasource": {"type": "loki", "uid": "logs"}},
                {"datasource": "Loki"},
                {"datasource": "$unknown_dynamic_source"},
            ],
        }

        self.assertEqual(
            prepare_dashboards.normalize_dashboard(deepcopy(dashboard)), dashboard
        )

    def test_preserves_existing_datasource_uid_without_database_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "grafana.db"
            self.assertEqual(prepare_dashboards.existing_prometheus_uid(database), "prometheus")
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE data_source (org_id INTEGER, name TEXT, type TEXT, uid TEXT)")
                connection.execute("INSERT INTO data_source VALUES (1, 'Prometheus', 'prometheus', 'existing-uid')")
            before = database.read_bytes()
            uid = prepare_dashboards.existing_prometheus_uid(database)
            self.assertEqual(uid, "existing-uid")
            self.assertEqual(database.read_bytes(), before)
            dashboard = {"panels": [{"datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}}]}
            result = prepare_dashboards.normalize_dashboard(dashboard, uid)
            self.assertEqual(result["panels"][0]["datasource"]["uid"], "existing-uid")

    def test_download_retries_http_and_invalid_json_then_fails(self):
        attempts = []
        http_error = HTTPError(
            "https://example.invalid", 503, "unavailable", {}, io.BytesIO()
        )
        responses = iter(
            [
                http_error,
                io.BytesIO(b"not-json"),
                io.BytesIO(b"[]"),
            ]
        )

        def fake_urlopen(_request, *, timeout):
            attempts.append(timeout)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with patch.object(prepare_dashboards, "urlopen", fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                prepare_dashboards.download_dashboard(1860, timeout=7)
        http_error.close()
        self.assertEqual(attempts, [7, 7, 7])

    def test_manifest_mounts_separate_script_config_and_dashboard_directory(self):
        documents = list(
            yaml.safe_load_all(
                (ROOT / "ansible/k8s/monitoring/grafana.yml").read_text()
            )
        )
        deployment = next(
            document for document in documents if document.get("kind") == "Deployment"
        )

        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate", "rollingUpdate": None})
        pod = deployment["spec"]["template"]["spec"]
        initializer = pod["initContainers"][0]
        self.assertTrue(initializer["image"].startswith("python:3.13."))
        self.assertEqual(
            initializer["command"],
            [
                "python",
                "/scripts/prepare_dashboards.py",
                "--output-dir",
                "/dashboards",
                "--database",
                "/existing-data/grafana.db",
                "--datasource-config-dir",
                "/datasources",
            ],
        )
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(
            volumes["dashboard-preparer"]["configMap"],
            {
                "name": "grafana-dashboard-preparer",
                "items": [
                    {"key": "prepare_dashboards.py", "path": "prepare_dashboards.py"}
                ],
            },
        )
        grafana = pod["containers"][0]
        mounts = {
            mount["name"]: mount["mountPath"] for mount in grafana["volumeMounts"]
        }
        self.assertEqual(
            mounts["dashboards-json"], "/var/lib/grafana/dashboards"
        )


if __name__ == "__main__":
    unittest.main()
