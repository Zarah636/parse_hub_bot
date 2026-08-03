import os
import unittest
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram.types import InputMediaVideo, Message  # noqa: E402

from plugins.parse.cache import build_cached_media_group, cache_media_from_message  # noqa: E402
from services import CacheMedia, CacheMediaType  # noqa: E402


class ParseCacheVideoCoverTests(unittest.TestCase):
    def test_sent_video_cover_file_id_is_saved_in_cache(self) -> None:
        message = cast(
            Message,
            SimpleNamespace(
                photo=None,
                video=SimpleNamespace(
                    file_id="video-file-id",
                    video_cover=SimpleNamespace(file_id="cover-file-id"),
                ),
                animation=None,
                document=None,
            ),
        )

        cached = cache_media_from_message(message)

        assert cached is not None
        self.assertEqual(cached.file_id, "video-file-id")
        self.assertEqual(cached.cover_file_id, "cover-file-id")

    def test_cached_video_cover_follows_current_setting(self) -> None:
        cached = CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id="video-file-id",
            cover_file_id="cover-file-id",
        )

        enabled = build_cached_media_group([cached], video_cover=True)
        disabled = build_cached_media_group([cached], video_cover=False)

        assert isinstance(enabled[0], InputMediaVideo)
        self.assertEqual(enabled[0].video_cover, "cover-file-id")
        assert isinstance(disabled[0], InputMediaVideo)
        self.assertIsNone(disabled[0].video_cover)


if __name__ == "__main__":
    unittest.main()
