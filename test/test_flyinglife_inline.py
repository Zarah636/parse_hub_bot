import os
import unittest
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from parsehub.types import VideoParseResult, VideoRef  # noqa: E402
from pyrogram.types import ChosenInlineResult, InlineQueryResultPhoto  # noqa: E402

from plugins.parse.inline import (  # noqa: E402
    build_inline_results,
    inline_result_download,
    resolve_inline_preview,
    resolve_inline_video_cover,
)
from repo.settings import SettingsConfig  # noqa: E402
from services.flyinglife import (  # noqa: E402
    FlyingLifeInlineCandidate,
    FlyingLifeUnavailable,
    flyinglife,
)
from services.media import ProcessedMedia  # noqa: E402
from services.pipeline import PipelineResult  # noqa: E402


class FakeSessionContext(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None


def build_candidate() -> FlyingLifeInlineCandidate:
    preview = VideoParseResult(
        video=VideoRef(
            url="https://v5-default.365yg.com/video.mp4",
            thumb_url="https://p11-sign.douyinpic.com/cover.webp",
        )
    )
    download = VideoParseResult(
        video=VideoRef(
            url="https://parse.flyinglife.cn/api/media_proxy.php?type=video",
            thumb_url="https://parse.flyinglife.cn/api/media_proxy.php?type=image",
        )
    )
    return FlyingLifeInlineCandidate(preview_result=preview, download_result=download)


class FlyingLifeInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_preview_keeps_cover_when_personal_video_cover_is_disabled(self) -> None:
        candidate = build_candidate()
        with patch("plugins.parse.inline.t_", {"test": lambda text: text}):
            results = await build_inline_results(
                candidate.preview_result,
                MagicMock(),
                "test",
                SettingsConfig(video_cover=False),
            )

        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], InlineQueryResultPhoto)
        assert isinstance(results[0], InlineQueryResultPhoto)
        self.assertEqual(results[0].photo_url, "https://p11-sign.douyinpic.com/cover.webp")

    async def test_preview_prefers_flyinglife_and_caches_both_candidates(self) -> None:
        candidate = build_candidate()
        with (
            patch("plugins.parse.inline.inline_flyinglife_cache.get", AsyncMock(return_value=None)),
            patch("plugins.parse.inline.inline_flyinglife_cache.set", AsyncMock()) as cache_set,
            patch.object(flyinglife, "can_attempt", return_value=True),
            patch.object(flyinglife, "parse_inline_candidate", AsyncMock(return_value=candidate)) as parse,
            patch("plugins.parse.inline.ParseService") as parse_service,
        ):
            result = await resolve_inline_preview(
                "https://v.douyin.com/example/", "https://www.douyin.com/video/1", "douyin"
            )

        self.assertIs(result, candidate.preview_result)
        parse.assert_awaited_once()
        cache_set.assert_awaited_once_with("https://www.douyin.com/video/1", candidate)
        parse_service.return_value.parse.assert_not_called()

    async def test_preview_falls_back_to_parsehub_after_flyinglife_failure(self) -> None:
        fallback = VideoParseResult(video=VideoRef(url="https://example.com/fallback.mp4"))
        parse_service = MagicMock()
        parse_service.parse = AsyncMock(return_value=fallback)
        with (
            patch("plugins.parse.inline.inline_flyinglife_cache.get", AsyncMock(return_value=None)),
            patch.object(flyinglife, "can_attempt", return_value=True),
            patch.object(
                flyinglife,
                "parse_inline_candidate",
                AsyncMock(side_effect=FlyingLifeUnavailable("timeout")),
            ),
            patch("plugins.parse.inline.parse_cache.get", AsyncMock(return_value=None)),
            patch("plugins.parse.inline.parse_cache.set", AsyncMock()) as parse_cache_set,
            patch("plugins.parse.inline.ParseService", return_value=parse_service),
        ):
            result = await resolve_inline_preview(
                "https://v.douyin.com/example/", "https://www.douyin.com/video/1", "douyin"
            )

        self.assertIs(result, fallback)
        parse_service.parse.assert_awaited_once_with("https://v.douyin.com/example/")
        parse_cache_set.assert_awaited_once_with("https://www.douyin.com/video/1", fallback)

    def test_inline_video_cover_follows_personal_setting(self) -> None:
        video_ref = VideoRef(url="https://example.com/video.mp4", thumb_url="https://example.com/cover.jpg")

        self.assertEqual(
            resolve_inline_video_cover(video_ref, SettingsConfig(video_cover=True)),
            "https://example.com/cover.jpg",
        )
        self.assertIsNone(resolve_inline_video_cover(video_ref, SettingsConfig(video_cover=False)))

    async def test_selected_result_uses_hybrid_pipeline_and_cover_setting(self) -> None:
        candidate = build_candidate()
        parsehub_cached = VideoParseResult(video=VideoRef(url="https://example.com/parsehub.mp4"))
        final_result = VideoParseResult(
            video=VideoRef(url="https://example.com/final.mp4", thumb_url="https://example.com/final-cover.jpg")
        )
        processed = cast(
            ProcessedMedia,
            SimpleNamespace(
                output_paths=[Path("video.mp4")],
                source=SimpleNamespace(path=Path("video.mp4")),
            ),
        )
        pipeline_result = PipelineResult(parse_result=final_result, processed_list=[processed], engine="flyinglife")
        pipeline = MagicMock()
        pipeline.__enter__.return_value = pipeline
        pipeline.run = AsyncMock(return_value=pipeline_result)

        cli = MagicMock()
        cli.edit_inline_media = AsyncMock()
        chosen_result = cast(
            ChosenInlineResult,
            SimpleNamespace(
                result_id="download_0",
                from_user=SimpleNamespace(id=123),
                inline_message_id="inline-message-id",
                query="https://v.douyin.com/example/",
            ),
        )
        settings_service = MagicMock()
        settings_service.get_config_by_user = AsyncMock(return_value=SettingsConfig(video_cover=False))
        user_service = MagicMock()
        user_service.get_lang = AsyncMock(return_value="test")
        parse_service = MagicMock()
        parse_service.get_platform.return_value = SimpleNamespace(id="douyin")
        parse_service.get_raw_url = AsyncMock(return_value="https://www.douyin.com/video/1")
        reporter = MagicMock()
        reporter.report = AsyncMock()
        reporter.report_error = AsyncMock()

        with (
            patch("plugins.parse.inline.get_session", return_value=FakeSessionContext()),
            patch("plugins.parse.inline.UserService", return_value=user_service),
            patch("plugins.parse.inline.SettingsService", return_value=settings_service),
            patch("plugins.parse.inline.t_", {"test": lambda text: text}),
            patch("plugins.parse.inline.ParseService", return_value=parse_service),
            patch("plugins.parse.inline.parse_cache.get", AsyncMock(return_value=parsehub_cached)),
            patch("plugins.parse.inline.inline_flyinglife_cache.get", AsyncMock(return_value=candidate)),
            patch("plugins.parse.inline.InlineStatusReporter", return_value=reporter),
            patch("plugins.parse.inline.HybridParsePipeline", return_value=pipeline) as hybrid_pipeline,
            patch("plugins.parse.inline.resolve_media_info", return_value=(1920, 1080, 10)),
        ):
            await inline_result_download(cli, chosen_result)

        kwargs = hybrid_pipeline.call_args.kwargs
        self.assertIs(kwargs["parse_result"], parsehub_cached)
        self.assertIs(kwargs["flyinglife_parse_result"], candidate.download_result)
        self.assertFalse(kwargs["download_video_cover"])
        cli.edit_inline_media.assert_awaited_once()
        media = cli.edit_inline_media.await_args.kwargs["media"]
        self.assertIsNone(media.video_cover)


if __name__ == "__main__":
    unittest.main()
