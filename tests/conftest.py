"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_review_skip(monkeypatch):
    """deploy.sh runs the suite with BOXLOFT_SKIP_REVIEW set when Alex OKs a skip. The review gate's tests must not
    see it (11-25: test_two_runs_at_once... read the skip, the gate passed, and the deploy failed). A test that wants
    a skip sets it itself."""
    monkeypatch.delenv('BOXLOFT_SKIP_REVIEW', raising=False)
    monkeypatch.delenv('BOXLOFT_NO_MODEL', raising=False)
