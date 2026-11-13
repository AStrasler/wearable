"""Test fixtures: a throwaway TimescaleDB container + a configured FastAPI TestClient.
Integration tests skip automatically if Docker is unavailable."""
import os
import shutil
import subprocess  # nosec B404
import time
import uuid
from pathlib import Path

import pytest

IMAGE = "timescale/timescaledb:2.17.2-pg16"
DB_NAME = "whoop_test"
DB_USER = "whoop"
DB_PASSWORD = "testpw"


def _resolve_docker() -> str | None:
    """Resolve Docker once; use the absolute executable for every test fixture call."""
    found = shutil.which("docker")
    if not found:
        return None
    path = Path(found).resolve()
    return str(path) if path.is_file() else None


_DOCKER = _resolve_docker()


def _docker_argv(*args: str) -> list[str]:
    if _DOCKER is None:
        raise FileNotFoundError("Docker executable unavailable")
    return [_DOCKER, *args]


def _docker_available():
    if _DOCKER is None:
        return False
    return subprocess.run(  # nosec B603: absolute Docker executable; fixed argv
        _docker_argv("info"), capture_output=True, shell=False
    ).returncode == 0


requires_docker = pytest.mark.skipif(not _docker_available(), reason="docker not available")


@pytest.fixture(scope="session")
def timescale_dsn():
    """Start a throwaway TimescaleDB container, apply init.sql, yield a DSN, tear down."""
    if not _docker_available():
        pytest.skip("docker not available")
    import psycopg

    name = f"whoop-test-db-{uuid.uuid4().hex[:8]}"
    # publish container 5432 on an ephemeral host port (-P picks a free one)
    subprocess.run(  # nosec B603: absolute Docker executable and argv list
        _docker_argv("run", "-d", "--rm", "--name", name,
        "-e", f"POSTGRES_DB={DB_NAME}", "-e", f"POSTGRES_USER={DB_USER}",
        "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", "-P", IMAGE,
        ), check=True, capture_output=True, shell=False)
    try:
        port = subprocess.run(  # nosec B603
            _docker_argv("port", name, "5432/tcp"), check=True, capture_output=True, text=True, shell=False
        ).stdout.strip().rsplit(":", 1)[1]
        dsn = f"postgresql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{port}/{DB_NAME}"
        # wait for readiness
        deadline = time.time() + 60
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3) as c:
                    c.execute("SELECT 1")
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        # apply schema
        init_sql = os.path.join(os.path.dirname(__file__), "..", "..", "db", "init.sql")
        with open(init_sql) as fh, psycopg.connect(dsn, autocommit=True) as c:
            c.execute(fh.read())
        yield dsn
    finally:
        subprocess.run(  # nosec B603: absolute Docker executable, fixed command
            _docker_argv("rm", "-f", name), capture_output=True, shell=False
        )


@pytest.fixture
def clean_db(timescale_dsn):
    """Truncate all data tables before each test for isolation."""
    import psycopg
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("TRUNCATE devices, raw_batches, hr_samples, rr_intervals, events, battery, "
                  "spo2_samples, skin_temp_samples, resp_samples, gravity_samples, "
                  "sleep_sessions, exercise_sessions, daily_metrics, profile CASCADE")
    return timescale_dsn
