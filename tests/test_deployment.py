"""Deployment artefacts (Phase N).

Static checks. They do not prove the image builds - Docker is unavailable here - but
they do prove the safety properties the artefacts are supposed to carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "deploy" / "docker-compose.yml").read_text()
UNIT = (ROOT / "deploy" / "atlas.service").read_text()


def test_dockerfile_runs_as_non_root() -> None:
    """A trading process has no reason to write outside its data directory."""
    assert "USER atlas" in DOCKERFILE
    assert "useradd" in DOCKERFILE


def test_dockerfile_does_not_default_to_trading() -> None:
    """Starting a trader must be a deliberate act, never an image default."""
    assert 'CMD ["--help"]' in DOCKERFILE
    assert 'CMD ["run"]' not in DOCKERFILE


def test_dockerfile_declares_a_healthcheck() -> None:
    assert "HEALTHCHECK" in DOCKERFILE
    assert "atlas" in DOCKERFILE and "health" in DOCKERFILE


def test_dockerfile_persists_state_in_a_volume() -> None:
    """Losing /data loses the ledger, audit chain and kill-switch state."""
    assert 'VOLUME ["/data"]' in DOCKERFILE
    assert "ATLAS_DATA_DIR=/data" in DOCKERFILE


def test_dockerfile_does_not_copy_secrets() -> None:
    ignore = (ROOT / ".dockerignore").read_text()
    assert ".env" in ignore
    assert "COPY .env" not in DOCKERFILE


@pytest.mark.parametrize("secret", ["ATLAS_BINANCE_API_KEY=", "ATLAS_BINANCE_API_SECRET="])
def test_no_credentials_baked_into_deployment_artefacts(secret: str) -> None:
    for text in (DOCKERFILE, COMPOSE, UNIT):
        assert secret not in text


def test_compose_restarts_and_persists() -> None:
    assert "restart: unless-stopped" in COMPOSE
    assert "atlas-data:/data" in COMPOSE


def test_compose_bounds_resources() -> None:
    """A runaway process must not take the host down with it."""
    assert "memory: 512M" in COMPOSE


def test_compose_reads_env_from_the_host() -> None:
    assert "env_file" in COMPOSE


def test_systemd_restarts_with_throttle() -> None:
    assert "Restart=always" in UNIT
    assert "RestartSec=" in UNIT
    assert "StartLimitBurst=" in UNIT, "a crash loop must be throttled"


def test_systemd_shuts_down_gracefully() -> None:
    """SIGINT so the scheduler's interrupt path runs rather than being killed."""
    assert "KillSignal=SIGINT" in UNIT
    assert "TimeoutStopSec=" in UNIT


def test_systemd_is_hardened() -> None:
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "RestrictSUIDSGID=true",
    ):
        assert directive in UNIT, f"missing {directive}"


def test_systemd_restricts_writable_paths() -> None:
    assert "ReadWritePaths=" in UNIT
