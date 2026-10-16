#!/usr/bin/env python3
"""Replicate Immich's NAS dataset without changing application mounts or files.

Run on TrueNAS with Python 3.11+ and root privileges. The destination pool and
parent dataset must exist; the destination leaf must be new. ZFS replication
preserves NFSv4 ACLs, xattrs, hard links, and hidden Immich marker files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

SOURCE_PROPERTY = "clorofilla:immich-copy-source"
PREFIX_PROPERTY = "clorofilla:immich-copy-prefix"
DATASET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9_.:-]+)*")
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")


class CopyError(RuntimeError):
    """A failed precondition, transfer, or verification."""


def run(
    arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Capture metadata queries and comparisons without invoking a shell."""
    result = subprocess.run(arguments, capture_output=True, text=True)
    if check and result.returncode:
        raise CopyError(f"{shlex.join(arguments)}\n{result.stderr.strip()}")
    return result


def pipeline(send: list[str], receive: list[str]) -> None:
    """Stream bytes directly and check both processes, including sender failures."""
    print(f"+ {shlex.join(send)} | {shlex.join(receive)}", flush=True)
    sender = subprocess.Popen(send, stdout=subprocess.PIPE)
    try:
        receiver = subprocess.run(receive, stdin=sender.stdout)
        assert sender.stdout is not None
        sender.stdout.close()
        sender_status = sender.wait()
    except BaseException:
        if sender.stdout is not None:
            sender.stdout.close()
        if sender.poll() is None:
            sender.terminate()
        sender.wait()
        raise
    if sender_status or receiver.returncode:
        raise CopyError(
            f"Transfer failed: send={sender_status}, receive={receiver.returncode}. "
            "No forced receive or snapshot deletion was attempted."
        )


class Copy:
    """Guard a full copy and subsequent incremental copies of one filesystem."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.source = args.source
        self.destination = args.destination
        self.prefix = args.snapshot_prefix

    def get(self, dataset: str, property_name: str) -> str:
        return run(
            ["zfs", "get", "-Hp", "-o", "value", property_name, dataset]
        ).stdout.strip()

    def exists(self, dataset: str) -> bool:
        return (
            run(
                [
                    "zfs",
                    "list",
                    "-H",
                    "-t",
                    "filesystem,snapshot",
                    "-o",
                    "name",
                    dataset,
                ],
                check=False,
            ).returncode
            == 0
        )

    def change(self, arguments: list[str]) -> None:
        print(f"+ {shlex.join(arguments)}", flush=True)
        if not self.args.dry_run:
            subprocess.run(arguments, check=True)

    def preflight(self) -> None:
        for value in [self.source, self.destination]:
            if not DATASET_PATTERN.fullmatch(value) or any(
                part in {".", ".."} for part in value.split("/")
            ):
                raise CopyError(f"Invalid dataset name: {value!r}")
        if "/" not in self.destination:
            raise CopyError(
                "Use a new leaf dataset inside the prepared destination pool."
            )
        if not PREFIX_PATTERN.fullmatch(self.prefix):
            raise CopyError("Invalid snapshot prefix.")
        pools = {self.source.split("/")[0], self.destination.split("/")[0]}
        if len(pools) != 2:
            raise CopyError("Source and destination must belong to different pools.")
        for pool in pools:
            health = run(
                ["zpool", "get", "-H", "-o", "value", "health", pool]
            ).stdout.strip()
            if health != "ONLINE":
                raise CopyError(f"Pool {pool} is not ONLINE: {health}")
        source_tree = run(
            ["zfs", "list", "-Hr", "-o", "name,type", self.source]
        ).stdout.splitlines()
        if len(source_tree) != 1 or source_tree[0].split() != [
            self.source,
            "filesystem",
        ]:
            raise CopyError("Source must be one filesystem without child datasets.")
        if self.get(self.source, "encryption") != "off":
            raise CopyError(
                "Encrypted source datasets need a separately reviewed replication policy."
            )
        if self.get(self.source, "mounted") != "yes":
            raise CopyError("Source must be mounted to verify its snapshot contents.")
        self.require_mountpoint(self.source)

    def require_mountpoint(self, dataset: str) -> Path:
        value = self.get(dataset, "mountpoint")
        if value in {"none", "legacy", "-"} or not value.startswith("/"):
            raise CopyError(
                f"Dataset {dataset} needs an absolute ZFS-managed mountpoint."
            )
        return Path(value)

    def receive_command(self) -> list[str]:
        # Inherit the destination's mountpoint. TrueNAS pools have altroot=/mnt;
        # passing an effective /mnt/... path as a raw property would double it.
        return [
            "zfs",
            "receive",
            "-u",
            "-x",
            "mountpoint",
            "-o",
            "canmount=noauto",
            "-o",
            "sharenfs=off",
            "-o",
            "sharesmb=off",
            "-o",
            f"{SOURCE_PROPERTY}={self.source}",
            "-o",
            f"{PREFIX_PROPERTY}={self.prefix}",
            self.destination,
        ]

    def transfer(self, send: list[str]) -> None:
        receive = self.receive_command()
        if self.args.dry_run:
            print(f"+ {shlex.join(send)} | {shlex.join(receive)}", flush=True)
        else:
            pipeline(send, receive)

    def seed(self) -> None:
        if self.exists(self.destination):
            raise CopyError(
                "Destination already exists; seed only accepts a new leaf dataset."
            )
        parent = self.destination.rsplit("/", 1)[0]
        if self.get(parent, "type") != "filesystem":
            raise CopyError("Destination parent must be an existing filesystem.")
        parent_mount = self.require_mountpoint(parent)
        self.check_paths(parent_mount / self.destination.rsplit("/", 1)[1])
        needed = int(self.get(self.source, "logicalreferenced"))
        available = int(self.get(parent, "available"))
        if available < needed:
            raise CopyError(
                f"Destination has {available / 2**30:.1f} GiB; allow at least {needed / 2**30:.1f} GiB."
            )
        snapshot = f"{self.source}@{self.prefix}-seed"
        if not self.exists(snapshot):
            self.change(["zfs", "snapshot", snapshot])
        else:
            print(f"Reusing existing seed snapshot: {snapshot}", flush=True)
        self.transfer(["zfs", "send", "-v", "-p", snapshot])
        if self.args.dry_run:
            print("Dry-run plan only; no snapshot or destination was created.")
        else:
            print(
                "Seed complete. Immich still uses its existing storage; destination is unmounted."
            )

    def check_paths(self, destination_mount: Path) -> None:
        source_mount = self.require_mountpoint(self.source)
        if (
            source_mount == destination_mount
            or source_mount in destination_mount.parents
            or destination_mount in source_mount.parents
        ):
            raise CopyError("Source and destination mountpoints overlap.")
        if destination_mount.is_symlink():
            raise CopyError("Destination mountpoint must not be a symlink.")
        if (
            not self.exists(self.destination)
            and destination_mount.exists()
            and any(destination_mount.iterdir())
        ):
            raise CopyError(
                "Destination mountpoint contains files; refusing to hide them with a new mount."
            )

    def latest_copy(self) -> tuple[str, str]:
        if (
            self.get(self.destination, SOURCE_PROPERTY) != self.source
            or self.get(self.destination, PREFIX_PROPERTY) != self.prefix
        ):
            raise CopyError(
                "Destination is not a copy of this source with this snapshot prefix."
            )
        tree = run(
            ["zfs", "list", "-Hr", "-o", "name", self.destination]
        ).stdout.splitlines()
        if tree != [self.destination]:
            raise CopyError("Destination must not have child datasets.")
        snapshots = run(
            [
                "zfs",
                "list",
                "-H",
                "-t",
                "snapshot",
                "-d",
                "1",
                "-o",
                "name",
                "-s",
                "createtxg",
                self.destination,
            ]
        ).stdout.splitlines()
        if not snapshots:
            raise CopyError("Destination has no completed copy snapshot.")
        destination_snapshot = snapshots[-1]
        tag = destination_snapshot.split("@", 1)[1]
        if tag != f"{self.prefix}-seed" and not tag.startswith(
            f"{self.prefix}-cutover-"
        ):
            raise CopyError(
                "The newest destination snapshot is not from this copy operation."
            )
        source_snapshot = f"{self.source}@{tag}"
        if self.get(source_snapshot, "guid") != self.get(destination_snapshot, "guid"):
            raise CopyError("Source and destination snapshot GUIDs do not match.")
        self.check_paths(self.require_mountpoint(self.destination))
        return source_snapshot, tag

    def sync(self) -> None:
        if not self.args.writers_stopped and not self.args.dry_run:
            raise CopyError(
                "Stop Immich and other source writers, then pass --writers-stopped."
            )
        previous_snapshot, _ = self.latest_copy()
        if self.get(self.destination, "mounted") != "no":
            raise CopyError(
                "Destination is mounted. Unmount it and stop destination writers before syncing."
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = f"{self.source}@{self.prefix}-cutover-{stamp}"
        self.change(["zfs", "snapshot", snapshot])
        self.transfer(["zfs", "send", "-v", "-p", "-i", previous_snapshot, snapshot])
        if self.args.dry_run:
            print("Dry-run plan only; no final snapshot or incremental copy was made.")
        else:
            print(
                "Final sync complete. Keep writers stopped and run verify before switching mounts."
            )

    def require_frozen(self, snapshot: str) -> None:
        if run(["zfs", "diff", "-H", snapshot]).stdout.strip():
            raise CopyError(
                f"{snapshot.split('@')[0]} changed after the copy snapshot. "
                "Stop writers and reconcile changes before continuing."
            )

    def verify(self) -> None:
        snapshot, tag = self.latest_copy()
        final = tag.startswith(f"{self.prefix}-cutover-")
        source_mount = self.require_mountpoint(self.source)
        destination_mount = self.require_mountpoint(self.destination)
        if final:
            self.require_frozen(snapshot)
        for property_name in ["acltype", "aclmode", "aclinherit"]:
            if self.get(self.source, property_name) != self.get(
                self.destination, property_name
            ):
                raise CopyError(f"Source and destination {property_name} differ.")
        if self.get(self.destination, "mounted") != "yes":
            self.change(["zfs", "mount", self.destination])
        source_snapshot_path = source_mount / ".zfs" / "snapshot" / tag
        destination_snapshot = f"{self.destination}@{tag}"
        if not self.args.dry_run:
            self.require_frozen(destination_snapshot)
            for directory in [
                "upload",
                "library",
                "thumbs",
                "encoded-video",
                "profile",
                "backups",
            ]:
                for root in [source_snapshot_path, destination_mount]:
                    if not (root / "ImmichData" / directory / ".immich").is_file():
                        raise CopyError(
                            f"Missing Immich marker: {root}/ImmichData/{directory}/.immich"
                        )
        if not self.args.full_checksum:
            print(
                f"Verified {snapshot}: matching snapshot GUID and ACL properties; "
                "ZFS send/receive validated the stream checksums."
            )
            if self.args.dry_run:
                print("Dry-run only; frozen destination and markers were not checked.")
            else:
                if final:
                    self.require_frozen(snapshot)
                print(
                    "Destination unchanged since replication; all six Immich markers present."
                )
            print(
                "No full file reread requested; use --full-checksum for an additional rsync comparison."
            )
            if not final:
                print(
                    "This verifies the seed only; a frozen final sync is still required."
                )
            return
        compare = [
            "rsync",
            "--archive",
            "--hard-links",
            "--numeric-ids",
            "--checksum",
            "--dry-run",
            "--delete",
            "--itemize-changes",
            "--exclude=/.zfs/",
            str(source_snapshot_path) + "/",
            str(destination_mount) + "/",
        ]
        print(f"+ {shlex.join(compare)}", flush=True)
        if self.args.dry_run:
            return
        differences = run(compare).stdout.strip()
        if differences:
            raise CopyError(
                "Verification found differences:\n"
                + "\n".join(differences.splitlines()[:30])
            )
        if final:
            self.require_frozen(snapshot)
        self.require_frozen(destination_snapshot)
        print(
            f"Verified {snapshot}: matching snapshot GUID, ACL properties, files and checksums."
        )
        print(f"Destination mounted at {destination_mount}; canmount remains noauto.")
        print(
            "No NFS export, readiness marker, Helm change, extraction, upload, or deletion was performed."
        )
        if not final:
            print(
                "This verifies the seed snapshot only. A frozen final sync is still required."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seed", "sync", "verify"])
    parser.add_argument(
        "--source",
        default="fast/1tb/photos",
        help="Source filesystem (default: %(default)s)",
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="New leaf dataset, e.g. main/data/photos",
    )
    parser.add_argument(
        "--snapshot-prefix",
        default="immich-hdd",
        help="Stable prefix reused across all phases",
    )
    parser.add_argument(
        "--writers-stopped",
        action="store_true",
        help="Confirm Immich and all other source writers are stopped (sync only)",
    )
    parser.add_argument(
        "--full-checksum",
        action="store_true",
        help="Additionally reread every file with rsync --checksum (verify only; may take hours)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect preconditions and print mutations without executing them",
    )
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise CopyError(
                "Run on the NAS with sudo python3; ZFS operations require root."
            )
        copy = Copy(args)
        copy.preflight()
        getattr(copy, args.operation)()
    except (CopyError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. Existing source data and snapshots were retained.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
