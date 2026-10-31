import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "db_backup.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
args="$*"
echo "$args" >> "$FAKE_DOCKER_LOG"
case "$args" in
  *head-object*)
    [[ "$args" == *"$FAIL_HEAD_FOR"* && -n "$FAIL_HEAD_FOR" ]] && exit 1
    exit 0 ;;
  *pg_dump*) echo "dump" ;;
  *"volume inspect"*) exit 0 ;;
  *"tar -czf"*) echo "archive" ;;
  *list-objects-v2*) echo "" ;;
esac
exit 0
"""


def _run(tmp_path: Path, fail_head_for: str = "") -> tuple[subprocess.CompletedProcess[str], str]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    docker = stub / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    backups = tmp_path / "backups"
    env = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "ENV_FILE": str(tmp_path / "missing.env"),
        "DB_BACKUP_LOCAL_DIR": str(backups),
        "DB_BACKUP_LOCK_FILE": str(tmp_path / "backup.lock"),
        "DB_BACKUP_DATABASES": "testdb",
        "DB_BACKUP_VOLUMES": "testvol",
        "SPACES_BUCKET": "bucket",
        "SPACES_ACCESS_KEY": "key",
        "SPACES_SECRET_KEY": "secret",
        "POSTGRES_PASSWORD": "pw",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAIL_HEAD_FOR": fail_head_for,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "run"], env=env, capture_output=True, text=True, timeout=60
    )
    state = (backups / "backup_state.env").read_text() if (backups / "backup_state.env").exists() else ""
    return result, state


def test_successful_run_archives_volumes_and_records_success(tmp_path: Path) -> None:
    result, state = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LAST_UPLOAD_STATUS=success" in state
    calls = (tmp_path / "docker.log").read_text()
    assert "testvol:/data:ro" in calls
    assert "testvol_" in calls and ".tar.gz" in calls


@pytest.mark.parametrize("target", ["testvol_", "testdb_"])
def test_failed_upload_verification_fails_the_run(tmp_path: Path, target: str) -> None:
    result, state = _run(tmp_path, fail_head_for=target)
    assert result.returncode == 1
    assert "LAST_UPLOAD_STATUS=partial" in state
    assert "verification failed" in result.stdout
