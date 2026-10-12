#!/usr/bin/env python3
"""Download Grafana dashboards and bind their Prometheus references."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DASHBOARD_IDS = (1860, 15202, 18283, 15757, 13332)
DOWNLOAD_URL = "https://grafana.com/api/dashboards/{dashboard_id}/revisions/latest/download"
PROMETHEUS_UID = "prometheus"
ATTEMPTS = 3
TIMEOUT_SECONDS = 15
_VARIABLE_REFERENCE = re.compile(r"^\$(?:\{(?P<braced>[^}]+)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))$")


def _variable_name(value: object) -> str | None:
    """Return the name when a value is exactly one Grafana variable reference."""
    if not isinstance(value, str):
        return None
    match = _VARIABLE_REFERENCE.fullmatch(value)
    if not match:
        return None
    return match.group("braced") or match.group("plain")


def _is_prometheus_variable(variable: object) -> bool:
    """Identify a datasource template variable restricted to Prometheus."""
    if not isinstance(variable, dict) or variable.get("type") != "datasource":
        return False
    query = variable.get("query")
    if isinstance(query, str):
        return query.casefold() == "prometheus"
    if isinstance(query, dict):
        plugin_id = query.get("pluginId") or query.get("type")
        return isinstance(plugin_id, str) and plugin_id.casefold() == "prometheus"
    return False


def existing_prometheus_uid(database: Path) -> str:
    """Read the existing datasource identity without modifying Grafana's database."""
    if not database.exists():
        return PROMETHEUS_UID
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT uid FROM data_source WHERE org_id = 1 AND name = ? AND type = ?",
            ("Prometheus", "prometheus"),
        ).fetchone()
    return row[0] if row and row[0] else PROMETHEUS_UID


def normalize_dashboard(dashboard: dict[str, Any], datasource_uid: str = PROMETHEUS_UID) -> dict[str, Any]:
    """Bind static Prometheus references while retaining dynamic variables."""
    prometheus_inputs = {"DS_PROMETHEUS"}
    for item in dashboard.get("__inputs", []):
        if not isinstance(item, dict) or item.get("type") != "datasource":
            continue
        plugin = item.get("pluginId") or item.get("pluginName")
        if isinstance(plugin, str) and plugin.casefold() == "prometheus":
            name = item.get("name")
            if isinstance(name, str):
                prometheus_inputs.add(name)

    prometheus_variables: set[str] = set()
    templating = dashboard.get("templating")
    variables = templating.get("list", []) if isinstance(templating, dict) else []
    for variable in variables:
        if not _is_prometheus_variable(variable):
            continue
        name = variable.get("name")
        if isinstance(name, str):
            prometheus_variables.add(name)
        variable["current"] = {
            "selected": True,
            "text": "Prometheus",
            "value": datasource_uid,
        }

    def normalize(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                normalize(item)
            return
        if not isinstance(value, dict):
            return

        datasource = value.get("datasource")
        if isinstance(datasource, dict):
            datasource_type = datasource.get("type")
            if isinstance(datasource_type, str) and datasource_type.casefold() == "prometheus":
                variable_name = _variable_name(datasource.get("uid"))
                if variable_name not in prometheus_variables:
                    datasource["uid"] = datasource_uid
        elif isinstance(datasource, str):
            variable_name = _variable_name(datasource)
            if datasource.casefold() == "prometheus" or variable_name in prometheus_inputs:
                value["datasource"] = datasource_uid

        for child in value.values():
            normalize(child)

    normalize(dashboard)
    return dashboard


def download_dashboard(dashboard_id: int, *, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Download and decode one dashboard, retrying a bounded number of times."""
    url = DOWNLOAD_URL.format(dashboard_id=dashboard_id)
    last_error: Exception | None = None
    for _attempt in range(ATTEMPTS):
        try:
            request = Request(url, headers={"User-Agent": "grafana-dashboard-provisioner/1"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("dashboard JSON must be an object")
            return payload
        except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            last_error = error
    raise RuntimeError(f"failed to download valid dashboard {dashboard_id} after {ATTEMPTS} attempts") from last_error


def prepare_dashboards(output_dir: Path, datasource_uid: str = PROMETHEUS_UID) -> None:
    """Download, normalize, and write the configured dashboards."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for dashboard_id in DASHBOARD_IDS:
        dashboard = normalize_dashboard(download_dashboard(dashboard_id), datasource_uid)
        destination = output_dir / f"{dashboard_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(dashboard, separators=(",", ":")) + "\n")
        temporary.replace(destination)
        print(f"Prepared dashboard {dashboard_id}")


def main() -> int:
    """Run the dashboard initializer command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--datasource-config-dir", type=Path)
    args = parser.parse_args()
    try:
        datasource_uid = existing_prometheus_uid(args.database) if args.database else PROMETHEUS_UID
        if args.datasource_config_dir:
            args.datasource_config_dir.mkdir(parents=True, exist_ok=True)
            config = {
                "apiVersion": 1,
                "datasources": [{
                    "name": "Prometheus", "uid": datasource_uid, "type": "prometheus",
                    "access": "proxy", "url": "http://prometheus.monitoring.svc.cluster.local:9090",
                    "isDefault": True, "editable": False,
                }],
            }
            # JSON is valid YAML for Grafana's provisioning loader.
            (args.datasource_config_dir / "datasources.yaml").write_text(json.dumps(config) + "\n")
        prepare_dashboards(args.output_dir, datasource_uid)
    except Exception as error:  # The init container must fail closed with a useful error.
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
