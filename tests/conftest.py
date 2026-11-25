"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_review_skip(monkeypatch):
    """Tests run with the review-skip switches cleared, whatever the shell has set: a test that reads one sets it
    itself (11-25: a suite run with a skip set let a review-gate test pass a proven bug)."""
    monkeypatch.delenv('BOXLOFT_SKIP_REVIEW', raising=False)
    monkeypatch.delenv('BOXLOFT_NO_MODEL', raising=False)
