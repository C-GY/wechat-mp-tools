"""Keep persistent OSS settings and receipts away from the real user profile."""
import pytest


@pytest.fixture(autouse=True)
def isolated_oss_profile(tmp_path, monkeypatch):
    from backend import oss

    monkeypatch.setattr(oss, "OSS_CONFIG_FILE", tmp_path / "profile" / "oss_config.json")
