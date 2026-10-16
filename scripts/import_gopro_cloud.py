#!/usr/bin/env python3
"""Import GoPro ZIPs one at a time on TrueNAS using the official Immich CLI.

Run as root. Archives are retained. Only this script's extracted staging files
are removed, after server checksums, original sizes, and album membership pass.
Interrupted batches can be resumed with the same arguments and state directory.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import time
from urllib.parse import urlencode, urlsplit
import zipfile

IMAGE = "ghcr.io/immich-app/immich-cli@sha256:ccccd3cc51763063b31c18b81c4a00d98acbc287dc5203267925b70c32285b32"
ALBUM = "GoPro Cloud"


class NextPhase(Exception):
    """A durable checkpoint allows the runner to start a fresh worker."""


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    names = set()
    for item in entries:
        name = item.filename
        if (
            Path(name).name != name
            or "\\" in name
            or name.startswith(".")
            or Path(name).suffix.lower() not in {".mp4", ".jpg", ".jpeg"}
            or name in names
            or item.is_dir()
            or stat.S_ISLNK(item.external_attr >> 16)
            or item.flag_bits & 1
        ):
            raise RuntimeError(f"Unexpected archive member: {name!r}")
        names.add(name)
    if not entries:
        raise RuntimeError("Empty archive")
    return entries


class Importer:
    def checkpoint(self, message):
        if self.args.step:
            raise NextPhase(message)

    def __init__(self, args):
        self.args = args
        key_path = args.api_key_file
        if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
            raise RuntimeError("API key file must have mode 600 or stricter")
        self.key = key_path.read_text().strip()
        self.docker = shutil.which("docker")
        if not self.docker:
            raise RuntimeError("Docker is required on the NAS")
        self.url = urlsplit(args.url)
        if self.url.scheme != "https" or self.url.path.rstrip("/") != "/api":
            raise RuntimeError("Expected an HTTPS URL ending in /api")
        self.args.stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.args.stage.is_symlink():
            raise RuntimeError("Staging directory cannot be a symlink")
        self.state_dir = args.stage / ".manifests"
        self.state_dir.mkdir(exist_ok=True, mode=0o700)
        self.owner = self.api("GET", "/users/me")["id"]

    def api(self, method, path, value=None):
        connection = http.client.HTTPSConnection(
            self.url.hostname,
            self.url.port or 443,
            context=ssl.create_default_context(),
            timeout=120,
        )
        if self.args.server_ip:
            connection._create_connection = (
                lambda address, timeout, source_address=None: socket.create_connection(
                    (self.args.server_ip, address[1]), timeout, source_address
                )
            )
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        body = None
        if value is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(value).encode()
        try:
            connection.request(method, "/api" + path, body, headers)
            response = connection.getresponse()
            data = response.read()
            if response.status >= 300:
                raise RuntimeError(f"Immich {method} {path}: HTTP {response.status}")
            return json.loads(data) if data else None
        finally:
            connection.close()

    def cli(self, stage, *, filename=None, dry_run=False):
        command = [
            self.docker,
            "run",
            "--rm",
            "--pull=never",
            "--user=0:0",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--mount",
            f"type=bind,src={stage},dst=/import,readonly",
            "--env",
            "IMMICH_API_KEY",
            "--env",
            "IMMICH_INSTANCE_URL",
        ]
        if self.args.server_ip:
            command += ["--add-host", f"{self.url.hostname}:{self.args.server_ip}"]
        command += [
            IMAGE,
            "upload",
            "--album-name",
            ALBUM,
            "--concurrency",
            "1",
            "--no-progress",
            "--json-output",
        ]
        if dry_run:
            command.append("--dry-run")
        command.append("/import/" + filename if filename else "/import")
        environment = os.environ | {
            "IMMICH_API_KEY": self.key,
            "IMMICH_INSTANCE_URL": self.args.url,
        }
        # Fall back to fork when TrueNAS rejects the fast spawn path. This
        # single-threaded program can safely use setsid in the child to force
        # fork. Only retry a failed spawn, never a CLI that actually ran.
        launch_options = {"close_fds": False}
        for attempt in range(3):
            try:
                subprocess.run(command, env=environment, check=True, **launch_options)
                return
            except OSError as error:
                if error.errno != errno.ENOSYS or attempt == 2:
                    raise
                print("Retrying Docker process creation (ENOSYS)", flush=True)
                launch_options = {"preexec_fn": os.setsid}
                time.sleep(1)

    def verify(self, records):
        albums = self.api(
            "GET", "/albums?" + urlencode({"name": ALBUM, "isOwned": "true"})
        )
        albums = [album for album in albums if album["albumName"] == ALBUM]
        if len(albums) != 1:
            raise RuntimeError(f"Expected exactly one owned {ALBUM!r} album")
        album_id = albums[0]["id"]
        results = self.api(
            "POST",
            "/assets/bulk-upload-check",
            {
                "assets": [
                    {"id": record["name"], "checksum": record["sha1"]}
                    for record in records
                ]
            },
        )["results"]
        checks = {item["id"]: item for item in results}
        for record in records:
            check = checks[record["name"]]
            if (
                not check.get("assetId")
                or check.get("isTrashed")
                or check.get("reason") != "duplicate"
            ):
                raise RuntimeError(f"No live server asset for {record['name']}")
            asset_id = check["assetId"]
            asset = self.api("GET", f"/assets/{asset_id}")
            if (
                asset["ownerId"] != self.owner
                or asset["isTrashed"]
                or asset["isOffline"]
                or base64.b64decode(asset["checksum"]).hex() != record["sha1"]
            ):
                raise RuntimeError(
                    f"Server asset verification failed: {record['name']}"
                )
            path = Path(asset["originalPath"])
            roots = {
                "/data": "ImmichData",
                "/external": "CameraPhotos",
                "/existing-uploads": "PreviousImmichUploads",
            }
            for mount, subdirectory in roots.items():
                if path.is_relative_to(mount):
                    physical = (
                        self.args.hdd_root / subdirectory / path.relative_to(mount)
                    ).resolve()
                    break
            else:
                raise RuntimeError(f"Unexpected original path: {path}")
            if (
                not physical.is_relative_to(self.args.hdd_root.resolve())
                or physical.stat().st_size != record["bytes"]
            ):
                raise RuntimeError(
                    f"HDD original missing or wrong size: {record['name']}"
                )
            memberships = self.api("GET", "/albums?" + urlencode({"assetId": asset_id}))
            if album_id not in {album["id"] for album in memberships}:
                raise RuntimeError(f"Asset missing from album: {record['name']}")
            record["asset_id"] = asset_id
        return album_id

    def batch(self, archive_path):
        state_path = self.state_dir / (archive_path.stem + ".json")
        state = (
            json.loads(state_path.read_text()) if state_path.exists() else {"files": []}
        )
        signature = {
            "path": str(archive_path),
            "bytes": archive_path.stat().st_size,
            "mtime_ns": archive_path.stat().st_mtime_ns,
        }
        if state.get("archive", signature) != signature:
            raise RuntimeError(f"Archive changed since previous run: {archive_path}")
        state["archive"] = signature
        stage = self.args.stage / archive_path.stem
        if state.get("verified"):
            self.verify(state["files"])
            print(f"Already verified: {archive_path.name}", flush=True)
            self.cleanup(stage, state["files"])
            return
        stage.mkdir(exist_ok=True, mode=0o700)
        if stage.is_symlink():
            raise RuntimeError("Batch staging directory cannot be a symlink")
        if not state.get("extracted"):
            with zipfile.ZipFile(archive_path) as archive:
                entries = members(archive)
                allowed = {entry.filename for entry in entries}
                if any(
                    path.name not in allowed | {name + ".partial" for name in allowed}
                    for path in stage.iterdir()
                ):
                    raise RuntimeError(
                        f"Unexpected files in staging directory: {stage}"
                    )
                needed = sum(
                    item.file_size
                    for item in entries
                    if not (stage / item.filename).exists()
                )
                if (
                    shutil.disk_usage(stage).free
                    < needed + self.args.reserve_gib * 2**30
                ):
                    raise RuntimeError(
                        "Insufficient SSD space including reserved headroom"
                    )
                print(
                    f"Extracting {archive_path.name}: {len(entries)} files, {sum(item.file_size for item in entries)/2**30:.1f} GiB",
                    flush=True,
                )
                previous = {item["name"]: item for item in state["files"]}
                state["files"] = []
                for item in entries:
                    target = stage / item.filename
                    if target.is_symlink():
                        raise RuntimeError(f"Staging symlink: {target}")
                    cached = previous.get(item.filename)
                    if (
                        target.exists()
                        and cached
                        and target.stat().st_size == item.file_size
                        and cached["crc32"] == item.CRC
                    ):
                        record = cached
                    else:
                        if target.exists():
                            raise RuntimeError(
                                f"Unrecognized existing staged file: {target}"
                            )
                        partial = target.with_name(target.name + ".partial")
                        partial.unlink(missing_ok=True)
                        digest = hashlib.sha1()
                        with archive.open(item) as source, partial.open(
                            "xb"
                        ) as destination:
                            while block := source.read(4 * 1024 * 1024):
                                destination.write(block)
                                digest.update(block)
                        os.utime(
                            partial, (time.mktime(item.date_time + (0, 0, -1)),) * 2
                        )
                        partial.replace(target)
                        record = {
                            "name": item.filename,
                            "bytes": item.file_size,
                            "crc32": item.CRC,
                            "sha1": digest.hexdigest(),
                        }
                    state["files"].append(record)
                    save(state_path, state)
                    print(
                        f"  Staged {item.filename} ({item.file_size/2**30:.2f} GiB)",
                        flush=True,
                    )
        if not state.get("extracted"):
            state["extracted"] = True
            save(state_path, state)
            self.checkpoint(
                f"Extraction complete: {archive_path.name}; starting fresh upload worker"
            )
        if (
            not state.get("dry_run_done")
            and not (self.state_dir / "upload-tested.json").exists()
        ):
            self.cli(stage, dry_run=True)
            state["dry_run_done"] = True
            save(state_path, state)
            self.checkpoint(f"Dry run complete: {archive_path.name}")
        # A genuine archive photo tests the managed upload before large videos.
        if not (self.state_dir / "upload-tested.json").exists():
            photo = next(
                (
                    record
                    for record in state["files"]
                    if Path(record["name"]).suffix.lower() in {".jpg", ".jpeg"}
                ),
                None,
            )
            if photo:
                self.cli(stage, filename=photo["name"])
                self.verify([photo])
                save(self.state_dir / "upload-tested.json", photo)
                print(
                    "Real photo upload verified on HDD and in GoPro Cloud", flush=True
                )
                self.checkpoint(
                    "Real photo upload verified; starting batch upload worker"
                )
        if not state.get("upload_done"):
            print(f"Uploading {archive_path.name}", flush=True)
            self.cli(stage)
            state["upload_done"] = True
            save(state_path, state)
            self.checkpoint(
                f"Upload complete: {archive_path.name}; starting verification worker"
            )
        try:
            state["album_id"] = self.verify(state["files"])
        except Exception:
            # The CLI can finish despite individual upload failures. A retry
            # must run its deduplicating upload again instead of getting stuck
            # repeating verification of an incomplete batch.
            state["upload_done"] = False
            save(state_path, state)
            raise
        state["verified"] = True
        save(state_path, state)
        self.cleanup(stage, state["files"])
        print(
            f"VERIFIED {archive_path.name}: {len(state['files'])} entries; staging reclaimed",
            flush=True,
        )
        self.checkpoint(f"Batch complete: {archive_path.name}")

    @staticmethod
    def cleanup(stage, records):
        if not stage.exists():
            return
        expected = {record["name"] for record in records}
        if stage.is_symlink() or any(
            path.name not in expected or path.is_symlink() for path in stage.iterdir()
        ):
            raise RuntimeError("Unexpected staging content; refusing cleanup")
        for name in expected:
            (stage / name).unlink(missing_ok=True)
        stage.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archives",
        type=Path,
        default=Path("/mnt/main/data/photos/CameraPhotos/GoProDump"),
    )
    parser.add_argument(
        "--stage",
        type=Path,
        default=Path("/mnt/fast/1tb/immich-import-staging/GoProCloud"),
    )
    parser.add_argument(
        "--hdd-root", type=Path, default=Path("/mnt/main/data/photos")
    )
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--url", default="https://photos.clorofilla.casa/api")
    parser.add_argument("--server-ip", default="192.168.1.221")
    parser.add_argument("--reserve-gib", type=int, default=64)
    parser.add_argument(
        "--step",
        action="store_true",
        help="Run one phase, then exit 75 for the shell runner to continue",
    )
    args = parser.parse_args()
    os.umask(0o077)
    archives = sorted(args.archives.glob("GoPro-*.zip"))
    if len(archives) != 6:
        raise RuntimeError(f"Expected six GoPro archives, found {len(archives)}")
    if args.stage.resolve().is_relative_to(
        args.archives.resolve()
    ) or args.stage.resolve().is_relative_to(args.hdd_root.resolve()):
        raise RuntimeError("Staging must be outside the archive and live media trees")
    args.stage.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.stage / ".import.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another importer already owns this staging directory"
            ) from error
        importer = Importer(args)
        try:
            for archive_path in archives:
                importer.batch(archive_path)
        except NextPhase as progress:
            print(str(progress), flush=True)
            return 75
    print(
        "COMPLETE: all six archives verified in GoPro Cloud; original ZIPs retained",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
