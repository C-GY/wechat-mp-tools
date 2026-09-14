"""Run monitor regressions without loading live user state into singletons.

Pass ordinary pytest arguments. MySQL integration remains separately opt-in via
COMPETITOR_MYSQL_INTEGRATION=1; its fixtures roll back their test rows.
"""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="monitor-tests-") as directory:
        root = Path(directory)
        from backend import config
        with patch.object(config, "DATA_DIR", root / "data"), patch.object(config, "OUTPUT_DIR", root / "output"):
            from backend import oss
            with patch.object(oss, "persistent_config_dir", return_value=root / "config"):
                from backend import competitor_monitor, channels  # noqa: F401
        import pytest
        return pytest.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
