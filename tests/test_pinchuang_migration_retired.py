"""The obsolete migration must never collapse batch snapshot history."""

import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import migrate_pinchuang_single_video as migration


class RetiredMigrationTests(unittest.TestCase):
    def test_programmatic_migration_refuses_before_accessing_database_or_backup(self):
        connection = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "按同步批次"):
            migration.migrate(connection, "test", Path("unused-backup"))
        self.assertEqual(connection.mock_calls, [])

    def test_cli_refuses_old_instructions_without_reading_credentials(self):
        for flags in ([], ["--apply", "--writers-stopped"]):
            with self.subTest(flags=flags):
                stderr = io.StringIO()
                with (
                    patch("sys.argv", ["migrate_pinchuang_single_video.py", "--config", "missing-config.json", *flags]),
                    patch("sys.stderr", stderr),
                    patch.object(Path, "read_text", side_effect=AssertionError("Must not read credentials")),
                ):
                    with self.assertRaises(SystemExit) as exit_context:
                        migration.main()
                self.assertNotEqual(exit_context.exception.code, 0)
                self.assertIn("按同步批次", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
