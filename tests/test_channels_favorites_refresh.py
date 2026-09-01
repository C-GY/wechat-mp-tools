import unittest
from pathlib import Path
from unittest.mock import patch

from backend import channels_refresh


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_SCRIPT = ROOT / "injection_scripts" / "src" / "automation.js"


class FavoritesRefreshTaskTests(unittest.TestCase):
    def setUp(self):
        channels_refresh.reset_refresh_task()

    def tearDown(self):
        channels_refresh.reset_refresh_task()

    def test_task_contains_each_valid_favorite_once(self):
        task, created = channels_refresh.start_refresh_task(
            [
                {"username": "author-a", "nickname": "A"},
                {"username": "author-a", "nickname": "duplicate"},
                {"username": "", "nickname": "invalid"},
                {"username": "author-b", "nickname": "B"},
            ]
        )

        self.assertTrue(created)
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["total_authors"], 2)

        command = channels_refresh.claim_refresh_command()
        self.assertEqual(
            command["authors"],
            [
                {"username": "author-a", "nickname": "A"},
                {"username": "author-b", "nickname": "B"},
            ],
        )

    def test_running_task_is_claimed_only_once(self):
        channels_refresh.start_refresh_task([{"username": "author-a"}])

        first = channels_refresh.claim_refresh_command()
        second = channels_refresh.claim_refresh_command()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(channels_refresh.get_refresh_status()["status"], "running")

    def test_stale_claim_can_be_recovered_by_a_new_wechat_page(self):
        with patch.object(channels_refresh.time, "time", return_value=100.0):
            channels_refresh.start_refresh_task([{"username": "author-a"}])
            channels_refresh.claim_refresh_command()

        with patch.object(
            channels_refresh.time,
            "time",
            return_value=100.0 + channels_refresh.COMMAND_STALE_SECONDS + 1,
        ):
            recovered = channels_refresh.claim_refresh_command()

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["authors"][0]["username"], "author-a")

    def test_progress_update_is_visible_and_completion_closes_command(self):
        task, _ = channels_refresh.start_refresh_task([{"username": "author-a"}])
        channels_refresh.claim_refresh_command()

        updated = channels_refresh.update_refresh_task(
            {
                "task_id": task["task_id"],
                "status": "running",
                "completed_authors": 1,
                "failed_authors": 0,
                "total_videos": 18,
                "current_nickname": "A",
                "message": "已完成 1/1",
            }
        )
        self.assertEqual(updated["total_videos"], 18)
        self.assertEqual(updated["message"], "已完成 1/1")

        completed = channels_refresh.update_refresh_task(
            {"task_id": task["task_id"], "status": "completed"}
        )
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(channels_refresh.claim_refresh_command())

    def test_starting_while_active_reuses_the_current_task(self):
        first, first_created = channels_refresh.start_refresh_task(
            [{"username": "author-a"}]
        )
        second, second_created = channels_refresh.start_refresh_task(
            [{"username": "author-b"}]
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second["task_id"], first["task_id"])


class FavoritesRefreshInjectionContractTests(unittest.TestCase):
    def test_injected_page_claims_and_reports_remote_refresh_commands(self):
        source = AUTOMATION_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("/__wx_channels_api/refresh-command", source)
        self.assertIn("/__wx_channels_api/refresh-progress", source)
        self.assertIn("refreshFavoriteAuthor", source)
        self.assertIn("WXU.API.finderUserPage", source)


if __name__ == "__main__":
    unittest.main()
