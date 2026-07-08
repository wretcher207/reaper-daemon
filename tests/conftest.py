"""Shared fixtures for the daemon test suite."""

import pytest


@pytest.fixture
def root(tmp_path):
    """A temp bridge root with the working folders the bridge expects."""
    for d in ("inbox", "outbox", "processing", "bridge"):
        (tmp_path / d).mkdir()
    return str(tmp_path)
