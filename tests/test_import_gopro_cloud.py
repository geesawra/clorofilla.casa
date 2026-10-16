"""Protect archive boundaries and verify-before-cleanup for the GoPro import."""

import importlib.util
import argparse
import errno
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/import_gopro_cloud.py"
SPEC = importlib.util.spec_from_file_location("import_gopro_cloud", SCRIPT)
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


@pytest.mark.parametrize(
    "name",
    [
        "../outside.MP4",
        "/outside.MP4",
        "folder/video.MP4",
        "file\\video.MP4",
        ".hidden.JPG",
        "unexpected.txt",
    ],
)
def test_extraction_rejects_unsafe_or_unexpected_members(tmp_path, name):
    archive_path = tmp_path / "test.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(name, b"media")
    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(RuntimeError, match="Unexpected archive member"):
            importer.members(archive)


def test_cleanup_preserves_unexpected_files_and_expected_files_on_failure(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "expected.MP4").write_bytes(b"media")
    (stage / "unrelated.txt").write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        importer.Importer.cleanup(stage, [{"name": "expected.MP4"}])
    assert (stage / "expected.MP4").read_bytes() == b"media"
    assert (stage / "unrelated.txt").read_bytes() == b"keep"


def test_cleanup_only_removes_named_staging_files(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "expected.MP4").write_bytes(b"media")
    archive = tmp_path / "original.zip"
    archive.write_bytes(b"retain archive")
    importer.Importer.cleanup(stage, [{"name": "expected.MP4"}])
    assert not stage.exists()
    assert archive.read_bytes() == b"retain archive"


def test_cleanup_refuses_symlinks(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"retain")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "expected.MP4").symlink_to(original)
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        importer.Importer.cleanup(stage, [{"name": "expected.MP4"}])
    assert original.read_bytes() == b"retain"


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.ENOSYS, "spawn failed"),
        subprocess.CalledProcessError(1, ["docker"]),
    ],
)
def test_cli_retries_only_a_failed_process_spawn(monkeypatch, tmp_path, failure):
    worker = importer.Importer.__new__(importer.Importer)
    worker.args = argparse.Namespace(
        server_ip="192.168.1.221", url="https://photos.example/api"
    )
    worker.url = urlsplit(worker.args.url)
    worker.key = "test-only"
    worker.docker = "/bin/docker"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert "test-only" not in command
        assert kwargs["env"]["IMMICH_API_KEY"] == "test-only"
        if len(calls) == 1:
            raise failure

    monkeypatch.setattr(importer.subprocess, "run", run)
    monkeypatch.setattr(importer.time, "sleep", lambda seconds: None)
    if isinstance(failure, OSError):
        worker.cli(tmp_path)
        assert len(calls) == 2
    else:
        with pytest.raises(subprocess.CalledProcessError):
            worker.cli(tmp_path)
        assert len(calls) == 1


def test_a_second_importer_is_rejected_before_authentication(tmp_path, monkeypatch):
    archives = tmp_path / "archives"
    archives.mkdir()
    for index in range(6):
        (archives / f"GoPro-{index}.zip").touch()
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--archives",
            str(archives),
            "--stage",
            str(stage),
            "--api-key-file",
            str(tmp_path / "absent-key"),
        ],
    )
    with (stage / ".import.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another importer"):
            importer.main()


def test_phase_checkpoints_avoid_spawning_after_extraction_and_repeating_uploads(
    tmp_path,
):
    archive_path = tmp_path / "GoPro-test.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("video.MP4", b"video data")
    worker = importer.Importer.__new__(importer.Importer)
    worker.args = argparse.Namespace(
        stage=tmp_path / "staging", reserve_gib=0, step=True
    )
    worker.args.stage.mkdir()
    worker.state_dir = worker.args.stage / ".manifests"
    worker.state_dir.mkdir()
    cli_calls = []
    verified = []
    worker.cli = lambda stage, **kwargs: cli_calls.append(kwargs)

    def verify(records):
        verified.append(True)
        return "test-album"

    worker.verify = verify
    state_path = worker.state_dir / "GoPro-test.json"
    with pytest.raises(importer.NextPhase, match="Extraction complete"):
        worker.batch(archive_path)
    assert cli_calls == []
    with pytest.raises(importer.NextPhase, match="Dry run complete"):
        worker.batch(archive_path)
    assert cli_calls == [{"dry_run": True}]
    with pytest.raises(importer.NextPhase, match="Upload complete"):
        worker.batch(archive_path)
    assert cli_calls == [{"dry_run": True}, {}]
    assert verified == []
    assert (worker.args.stage / "GoPro-test/video.MP4").exists()

    def incomplete(records):
        raise RuntimeError("Missing upload")

    worker.verify = incomplete
    with pytest.raises(RuntimeError, match="Missing upload"):
        worker.batch(archive_path)
    assert json.loads(state_path.read_text())["upload_done"] is False
    assert (worker.args.stage / "GoPro-test/video.MP4").exists()
    worker.verify = verify
    with pytest.raises(importer.NextPhase, match="Upload complete"):
        worker.batch(archive_path)
    with pytest.raises(importer.NextPhase, match="Batch complete"):
        worker.batch(archive_path)
    assert verified == [True]
    assert not (worker.args.stage / "GoPro-test").exists()
    worker.batch(archive_path)
    assert cli_calls == [{"dry_run": True}, {}, {}]
