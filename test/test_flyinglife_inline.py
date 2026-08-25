import os
import tempfile
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
    call_inline_parse,
    inline_result_download,
    is_inline_cache_ready,
    resolve_inline_preview,
    resolve_inline_video_cover,
)
from repo.settings import SettingsConfig  # noqa: E402
from services import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult  # noqa: E402
from services.flyinglife import (  # noqa: E402
    FlyingLifeInlineCandidate,
    FlyingLifeUnavailable,
    flyinglife,
)
from services.media import ProcessedMedia, create_video_thumbnail  # noqa: E402
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
    async def test_video_thumbnail_is_extracted_from_a_nonzero_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "long-video.mp4"
            video_path.write_bytes(b"video")

            async def fake_run_cmd(*args: str, **_: Any) -> str:
                self.assertIn("10.000", args)
                Path(args[-1]).write_bytes(b"jpeg-thumbnail")
                return ""

            with patch("services.media.run_cmd", side_effect=fake_run_cmd):
                thumbnail = await create_video_thumbnail(video_path, duration=350)

            assert thumbnail is not None
            self.assertTrue(thumbnail.is_file())
            self.assertLessEqual(thumbnail.stat().st_size, 200 * 1024)

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
            patch.object(flyinglife, "should_attempt", return_value=True),
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
            patch.object(flyinglife, "should_attempt", return_value=True),
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

    async def test_disabled_policy_ignores_flyinglife_candidate_cache(self) -> None:
        fallback = VideoParseResult(video=VideoRef(url="https://example.com/fallback.mp4"))
        candidate_cache_get = AsyncMock(return_value=build_candidate())
        with (
            patch.object(flyinglife, "should_attempt", return_value=False),
            patch("plugins.parse.inline.inline_flyinglife_cache.get", candidate_cache_get),
            patch("plugins.parse.inline.parse_cache.get", AsyncMock(return_value=fallback)),
        ):
            result = await resolve_inline_preview(
                "https://v.douyin.com/example/", "https://www.douyin.com/video/1", "douyin"
            )

        self.assertIs(result, fallback)
        candidate_cache_get.assert_not_awaited()

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
        cache_media = CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id="cached-inline-video-file-id",
            has_thumbnail=True,
        )

        with (
            patch("plugins.parse.inline.get_session", return_value=FakeSessionContext()),
            patch("plugins.parse.inline.UserService", return_value=user_service),
            patch("plugins.parse.inline.SettingsService", return_value=settings_service),
            patch("plugins.parse.inline.t_", {"test": lambda text: text}),
            patch("plugins.parse.inline.ParseService", return_value=parse_service),
            patch("plugins.parse.inline.parse_cache.get", AsyncMock(return_value=parsehub_cached)),
            patch.object(flyinglife, "should_attempt", return_value=True),
            patch("plugins.parse.inline.inline_flyinglife_cache.get", AsyncMock(return_value=candidate)),
            patch("plugins.parse.inline.InlineStatusReporter", return_value=reporter),
            patch("plugins.parse.inline.HybridParsePipeline", return_value=pipeline) as hybrid_pipeline,
            patch("plugins.parse.inline.resolve_media_info", return_value=(1920, 1080, 10)),
            patch(
                "plugins.parse.inline.create_video_thumbnail",
                AsyncMock(return_value=Path("thumb.jpg")),
            ),
            patch(
                "plugins.parse.inline.upload_inline_video_for_cache",
                AsyncMock(return_value=cache_media),
            ) as upload_for_cache,
            patch("plugins.parse.inline.persistent_cache.set", AsyncMock()) as persistent_cache_set,
        ):
            await inline_result_download(cli, chosen_result)

        kwargs = hybrid_pipeline.call_args.kwargs
        self.assertIs(kwargs["parse_result"], parsehub_cached)
        self.assertIs(kwargs["flyinglife_parse_result"], candidate.download_result)
        self.assertFalse(kwargs["download_video_cover"])
        parse_service.get_raw_url.assert_awaited_once_with("https://v.douyin.com/example/")
        cli.edit_inline_media.assert_awaited_once()
        media = cli.edit_inline_media.await_args.kwargs["media"]
        self.assertIsNone(media.video_cover)
        self.assertEqual(media.media, "cached-inline-video-file-id")
        upload_for_cache.assert_awaited_once_with(
            cli,
            "video.mp4",
            thumbnail_path="thumb.jpg",
            width=1920,
            height=1080,
            duration=10,
        )
        persistent_cache_set.assert_awaited_once()
        assert persistent_cache_set.await_args is not None
        cache_key, cache_entry = persistent_cache_set.await_args.args
        self.assertEqual(cache_key, "https://www.douyin.com/video/1")
        self.assertEqual(cache_entry.media, [cache_media])

    async def test_bilibili_selected_result_writes_parse_and_file_id_caches(self) -> None:
        raw_url = "https://www.bilibili.com/video/BV1example"
        preview = VideoParseResult(video=VideoRef(url="https://example.com/preview.mp4"))
        final_result = VideoParseResult(
            title="Bilibili video",
            video=VideoRef(url="https://example.com/final.mp4"),
        )
        processed = cast(
            ProcessedMedia,
            SimpleNamespace(
                output_paths=[Path("bilibili.mp4")],
                source=SimpleNamespace(path=Path("bilibili.mp4")),
            ),
        )
        pipeline_result = PipelineResult(parse_result=final_result, processed_list=[processed], engine="parsehub")
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
                query=raw_url,
            ),
        )
        settings_service = MagicMock()
        settings_service.get_config_by_user = AsyncMock(return_value=SettingsConfig(video_cover=True))
        user_service = MagicMock()
        user_service.get_lang = AsyncMock(return_value="test")
        parse_service = MagicMock()
        parse_service.get_platform.return_value = SimpleNamespace(id="bilibili")
        parse_service.get_raw_url = AsyncMock(return_value=raw_url)
        reporter = MagicMock()
        reporter.report = AsyncMock()
        reporter.report_error = AsyncMock()
        cache_media = CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id="bilibili-video-file-id",
            has_thumbnail=True,
        )

        with (
            patch("plugins.parse.inline.get_session", return_value=FakeSessionContext()),
            patch("plugins.parse.inline.UserService", return_value=user_service),
            patch("plugins.parse.inline.SettingsService", return_value=settings_service),
            patch("plugins.parse.inline.t_", {"test": lambda text: text}),
            patch("plugins.parse.inline.ParseService", return_value=parse_service),
            patch("plugins.parse.inline.parse_cache.get", AsyncMock(return_value=preview)),
            patch("plugins.parse.inline.parse_cache.set", AsyncMock()) as parse_cache_set,
            patch.object(flyinglife, "should_attempt", return_value=False),
            patch("plugins.parse.inline.inline_flyinglife_cache.get", AsyncMock()) as candidate_cache_get,
            patch("plugins.parse.inline.InlineStatusReporter", return_value=reporter),
            patch("plugins.parse.inline.HybridParsePipeline", return_value=pipeline) as hybrid_pipeline,
            patch("plugins.parse.inline.resolve_media_info", return_value=(1280, 720, 30)),
            patch(
                "plugins.parse.inline.create_video_thumbnail",
                AsyncMock(return_value=Path("bilibili-thumb.jpg")),
            ),
            patch(
                "plugins.parse.inline.upload_inline_video_for_cache",
                AsyncMock(return_value=cache_media),
            ),
            patch("plugins.parse.inline.persistent_cache.set", AsyncMock()) as persistent_cache_set,
        ):
            await inline_result_download(cli, chosen_result)

        candidate_cache_get.assert_not_awaited()
        self.assertTrue(hybrid_pipeline.call_args.kwargs["skip_flyinglife"])
        parse_cache_set.assert_awaited_once_with(raw_url, final_result)
        cli.edit_inline_media.assert_awaited_once()
        self.assertEqual(cli.edit_inline_media.await_args.kwargs["media"].media, "bilibili-video-file-id")
        persistent_cache_set.assert_awaited_once()
        assert persistent_cache_set.await_args is not None
        cache_key, cache_entry = persistent_cache_set.await_args.args
        self.assertEqual(cache_key, raw_url)
        self.assertEqual(cache_entry.parse_result.title, "Bilibili video")
        self.assertEqual(cache_entry.media, [cache_media])

    def test_cached_inline_video_requires_embedded_thumbnail(self) -> None:
        without_thumbnail = CacheMedia(type=CacheMediaType.VIDEO, file_id="old-video-file-id")
        with_thumbnail = CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id="new-video-file-id",
            has_thumbnail=True,
        )

        parse_result = CacheParseResult()
        self.assertFalse(is_inline_cache_ready(CacheEntry(parse_result=parse_result, media=[without_thumbnail])))
        self.assertTrue(is_inline_cache_ready(CacheEntry(parse_result=parse_result, media=[with_thumbnail])))

    async def test_inline_query_removes_legacy_video_cache_without_thumbnail(self) -> None:
        raw_url = "https://www.bilibili.com/video/BV1legacy"
        stale = CacheEntry(
            parse_result=CacheParseResult(title="legacy"),
            media=[CacheMedia(type=CacheMediaType.VIDEO, file_id="legacy-file-id")],
        )
        preview = VideoParseResult(video=VideoRef(url="https://example.com/video.mp4"))
        inline_query = MagicMock()
        inline_query.query = raw_url
        inline_query.from_user = SimpleNamespace(id=123)
        inline_query.answer = AsyncMock()
        parse_service = MagicMock()
        parse_service.get_platform.return_value = SimpleNamespace(id="bilibili")
        parse_service.get_raw_url = AsyncMock(return_value=raw_url)
        user_service = MagicMock()
        user_service.get_lang = AsyncMock(return_value="test")
        settings_service = MagicMock()
        settings_service.get_config_by_user = AsyncMock(return_value=SettingsConfig())

        with (
            patch("plugins.parse.inline.get_session", return_value=FakeSessionContext()),
            patch("plugins.parse.inline.UserService", return_value=user_service),
            patch("plugins.parse.inline.SettingsService", return_value=settings_service),
            patch("plugins.parse.inline.ParseService", return_value=parse_service),
            patch("plugins.parse.inline.persistent_cache.get", AsyncMock(return_value=stale)),
            patch("plugins.parse.inline.persistent_cache.remove", AsyncMock()) as cache_remove,
            patch("plugins.parse.inline.resolve_inline_preview", AsyncMock(return_value=preview)),
            patch("plugins.parse.inline.build_inline_results", AsyncMock(return_value=[])),
        ):
            await call_inline_parse(MagicMock(), inline_query)

        cache_remove.assert_awaited_once_with(raw_url)
        inline_query.answer.assert_awaited_once_with([], cache_time=0)


if __name__ == "__main__":
    unittest.main()
