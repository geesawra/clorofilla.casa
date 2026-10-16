"""Exercise copy guards and pipeline failures without modifying a ZFS pool."""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/copy_immich_storage.py"
SPEC = importlib.util.spec_from_file_location("copy_immich_storage", SCRIPT)
copy_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(copy_module)


def copier(**overrides):
    args = {
        "source": "fast/1tb/photos",
        "destination": "main/data/photos",
        "snapshot_prefix": "immich-hdd",
        "dry_run": False,
        "writers_stopped": False,
        "full_checksum": False,
    } | overrides
    return copy_module.Copy(argparse.Namespace(**args))


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "fast/../photos"},
        {"destination": "--force"},
        {"destination": "main"},
        {"destination": "fast/other-photos"},
        {"snapshot_prefix": "bad/name"},
    ],
)
def test_invalid_destinations_fail_before_any_zfs_operation(overrides, monkeypatch):
    monkeypatch.setattr(
        copy_module, "run", lambda *args, **kwargs: pytest.fail("ZFS was called")
    )
    with pytest.raises(copy_module.CopyError):
        copier(**overrides).preflight()


def test_seed_never_overwrites_an_existing_dataset(monkeypatch):
    copy = copier()
    monkeypatch.setattr(copy, "exists", lambda dataset: True)
    monkeypatch.setattr(
        copy, "change", lambda command: pytest.fail("Mutation was attempted")
    )
    with pytest.raises(copy_module.CopyError, match="already exists"):
        copy.seed()


def test_seed_dry_run_inherits_mountpoint_without_creating_snapshots(
    monkeypatch, capsys
):
    copy = copier(dry_run=True)
    properties = {
        ("main/data", "type"): "filesystem",
        ("main/data", "mountpoint"): "/mnt/main/data",
        ("main/data", "available"): str(2**40),
        (copy.source, "logicalreferenced"): str(680 * 2**30),
        (copy.source, "mountpoint"): "/mnt/fast/1tb/photos",
    }
    monkeypatch.setattr(copy, "get", lambda dataset, prop: properties[dataset, prop])
    monkeypatch.setattr(copy, "exists", lambda dataset: False)
    monkeypatch.setattr(
        copy_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Mutation was attempted"),
    )
    monkeypatch.setattr(
        copy_module, "pipeline", lambda *args: pytest.fail("Transfer was attempted")
    )
    copy.seed()
    output = capsys.readouterr().out
    assert "zfs snapshot fast/1tb/photos@immich-hdd-seed" in output
    assert "-x mountpoint" in output
    assert "canmount=noauto" in output
    assert "-F" not in output
    assert "Dry-run plan only" in output


def test_sync_requires_stopped_writers_and_an_unmounted_destination(monkeypatch):
    copy = copier()
    with pytest.raises(copy_module.CopyError, match="writers-stopped"):
        copy.sync()
    copy.args.writers_stopped = True
    monkeypatch.setattr(
        copy,
        "latest_copy",
        lambda: ("fast/1tb/photos@immich-hdd-seed", "immich-hdd-seed"),
    )
    monkeypatch.setattr(copy, "get", lambda *args: "yes")
    monkeypatch.setattr(
        copy, "change", lambda command: pytest.fail("Mutation was attempted")
    )
    with pytest.raises(copy_module.CopyError, match="mounted"):
        copy.sync()


def test_an_unrelated_destination_is_never_synced(monkeypatch):
    copy = copier(writers_stopped=True)
    monkeypatch.setattr(copy, "get", lambda *args: "unrelated/source")
    monkeypatch.setattr(
        copy, "change", lambda command: pytest.fail("Mutation was attempted")
    )
    with pytest.raises(copy_module.CopyError, match="not a copy"):
        copy.sync()


def test_snapshot_guid_mismatch_blocks_sync(monkeypatch):
    copy = copier(writers_stopped=True)

    def get(dataset, prop):
        if prop == copy_module.SOURCE_PROPERTY:
            return copy.source
        if prop == copy_module.PREFIX_PROPERTY:
            return copy.prefix
        if prop == "guid":
            return "source-guid" if dataset.startswith("fast/") else "different-guid"
        pytest.fail(f"Unexpected property: {prop}")

    def run(arguments, **kwargs):
        value = copy.destination
        if "snapshot" in arguments:
            value += "@immich-hdd-seed"
        return subprocess.CompletedProcess(arguments, 0, value + "\n", "")

    monkeypatch.setattr(copy, "get", get)
    monkeypatch.setattr(copy_module, "run", run)
    with pytest.raises(copy_module.CopyError, match="GUIDs"):
        copy.latest_copy()


def test_source_changes_after_final_snapshot_block_verification(monkeypatch):
    copy = copier()
    monkeypatch.setattr(
        copy_module,
        "run",
        lambda args: subprocess.CompletedProcess(args, 0, "M /some/file\n", ""),
    )
    with pytest.raises(copy_module.CopyError, match="changed after"):
        copy.require_frozen("fast/1tb/photos@immich-hdd-cutover-test")


@pytest.mark.parametrize("full_checksum", [False, True])
def test_full_reread_is_optional_and_markers_are_required(
    tmp_path, monkeypatch, full_checksum
):
    copy = copier(full_checksum=full_checksum)
    tag = "immich-hdd-cutover-test"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    snapshot_root = source / ".zfs" / "snapshot" / tag
    for root in [snapshot_root, destination]:
        for name in [
            "upload",
            "library",
            "thumbs",
            "encoded-video",
            "profile",
            "backups",
        ]:
            directory = root / "ImmichData" / name
            directory.mkdir(parents=True)
            (directory / ".immich").touch()
    monkeypatch.setattr(copy, "latest_copy", lambda: (f"{copy.source}@{tag}", tag))
    monkeypatch.setattr(
        copy,
        "require_mountpoint",
        lambda dataset: source if dataset == copy.source else destination,
    )
    monkeypatch.setattr(
        copy, "get", lambda dataset, prop: "yes" if prop == "mounted" else "passthrough"
    )
    commands = []

    def run(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(copy_module, "run", run)
    copy.verify()
    comparisons = [command for command in commands if command[0] == "rsync"]
    assert bool(comparisons) is full_checksum
    assert any(command[-1] == f"{copy.destination}@{tag}" for command in commands)
    if comparisons:
        assert "--checksum" in comparisons[0]
        assert "--dry-run" in comparisons[0]
    (destination / "ImmichData/upload/.immich").unlink()
    with pytest.raises(copy_module.CopyError, match="Missing Immich marker"):
        copy.verify()


@pytest.mark.parametrize("sender_status,receiver_status", [(7, 0), (0, 9)])
def test_pipeline_checks_both_sides(sender_status, receiver_status):
    sender = [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.buffer.write(b'copy'); sys.exit({sender_status})",
    ]
    receiver = [
        sys.executable,
        "-c",
        f"import sys; sys.stdin.buffer.read(); sys.exit({receiver_status})",
    ]
    with pytest.raises(copy_module.CopyError, match="Transfer failed"):
        copy_module.pipeline(sender, receiver)


def test_pipeline_streams_bytes_to_receiver(tmp_path):
    destination = tmp_path / "received"
    sender = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'copy-data')"]
    receiver = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
        str(destination),
    ]
    copy_module.pipeline(sender, receiver)
    assert destination.read_bytes() == b"copy-data"
