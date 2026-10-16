#!/usr/bin/env bash
# Keep extraction and each CLI launch in separate, resumable Python processes.
set -euo pipefail
umask 077
gopro_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec 9>/run/lock/immich-gopro-import.lock
if ! flock -n 9; then
  echo 'Another GoPro import runner is active.' >&2
  exit 1
fi
while true; do
  if python3 -u "$gopro_script_dir/import_gopro_cloud.py" --step "$@"; then
    exit 0
  else
    gopro_status=$?
  fi
  if [[ "$gopro_status" -ne 75 ]]; then
    echo "FAILED: import worker exited $gopro_status; staging and ZIPs retained." >&2
    exit "$gopro_status"
  fi
done
