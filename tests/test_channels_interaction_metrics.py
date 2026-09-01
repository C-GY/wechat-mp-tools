import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import channels
from backend import mitm_proxy


class ChannelsInteractionMetricsPersistenceTests(unittest.TestCase):
    def test_synced_author_feed_persists_all_four_interaction_counts(self):
        raw_feed = {
            "id": "feed-1",
            "createtime": 1700000000,
            "likeCount": 1234,
            "forwardCount": 56,
            "favCount": 78,
            "commentCount": 90,
            "contact": {
                "username": "author-a",
                "nickname": "Author A",
                "headUrl": "",
            },
            "objectDesc": {
                "mediaType": 4,
                "description": "sample video",
                "media": [
                    {
                        "url": "https://example.invalid/video.mp4?token=1",
                        "urlToken": "",
                        "coverUrl": "https://example.invalid/cover.jpg",
                        "decodeKey": "key",
                        "videoPlayLen": 125.8,
                        "spec": [],
                    }
                ],
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            feeds_file = Path(temp_dir) / "feeds.json"
            favorites_file = Path(temp_dir) / "favorites.json"
            with (
                patch.object(channels, "CHANNELS_FEEDS_FILE", feeds_file),
                patch.object(channels, "CHANNELS_FAVORITES_FILE", favorites_file),
            ):
                mitm_proxy.save_synced_feeds("author-a", [raw_feed])

            stored = json.loads(feeds_file.read_text(encoding="utf-8"))
            video = stored["author-a"][0]
            self.assertEqual(video["like_count"], 1234)
            self.assertEqual(video["share_count"], 56)
            self.assertEqual(video["favorite_count"], 78)
            self.assertEqual(video["comment_count"], 90)
            self.assertEqual(video["duration_seconds"], 125)


if __name__ == "__main__":
    unittest.main()
