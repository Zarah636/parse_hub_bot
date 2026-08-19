import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from plugins.parse import handlers  # noqa: E402
from repo.settings import ParseMode  # noqa: E402


class FlyingLifeHandlerTests(unittest.IsolatedAsyncioTestCase):
    def build_request(self) -> SimpleNamespace:
        return SimpleNamespace(
            cli=MagicMock(),
            url="https://v.douyin.com/example/",
            mode=ParseMode.PREVIEW,
            bypass_cache=False,
            delete_share_url_msg=False,
            msg=MagicMock(id=123),
            config=MagicMock(),
            t_=lambda text: text,
            chat_id=-1001,
        )

    async def run_until_pipeline(
        self,
        *,
        use_flyinglife: bool,
        raw_url: str = "https://www.douyin.com/video/123",
        mode: ParseMode = ParseMode.PREVIEW,
        video_cover: bool = True,
    ) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
        req = self.build_request()
        req.mode = mode
        req.config.video_cover = video_cover
        parse_service = MagicMock()
        parse_service.get_platform.return_value = SimpleNamespace(id="douyin")
        parse_service.get_raw_url = AsyncMock(return_value=raw_url)

        reporter = MagicMock()
        reporter.report = AsyncMock()
        reporter.report_error = AsyncMock()
        reporter.dismiss = AsyncMock()

        pipeline = MagicMock()
        pipeline.__enter__.return_value = pipeline
        pipeline.__exit__.return_value = None
        pipeline.run = AsyncMock(return_value=None)
        pipeline.waited = False

        with (
            patch.object(handlers, "ParseService", return_value=parse_service),
            patch.object(handlers.flyinglife, "should_attempt", return_value=use_flyinglife),
            patch.object(handlers, "MessageStatusReporter", return_value=reporter),
            patch.object(handlers, "MessageSender", return_value=MagicMock()),
            patch.object(handlers.persistent_cache, "get", AsyncMock(return_value=None)),
            patch.object(handlers.parse_cache, "get", AsyncMock(return_value=None)),
            patch.object(handlers, "HybridParsePipeline", return_value=pipeline) as hybrid_pipeline,
        ):
            await handlers.handle_parse.__wrapped__(req)  # type: ignore[attr-defined]

        return req, parse_service, hybrid_pipeline

    async def test_flyinglife_uses_original_canonical_cache_identity(self) -> None:
        req, parse_service, hybrid_pipeline = await self.run_until_pipeline(use_flyinglife=True)

        parse_service.get_raw_url.assert_awaited_once_with(req.url)
        args = hybrid_pipeline.call_args.args
        kwargs = hybrid_pipeline.call_args.kwargs
        self.assertEqual(args[1], "https://www.douyin.com/video/123")
        self.assertEqual(kwargs["platform_id"], "douyin")
        self.assertEqual(kwargs["download_video_cover"], req.config.video_cover)

    async def test_disabled_video_cover_is_passed_to_flyinglife(self) -> None:
        _, _, hybrid_pipeline = await self.run_until_pipeline(use_flyinglife=True, video_cover=False)
        self.assertFalse(hybrid_pipeline.call_args.kwargs["download_video_cover"])

    async def test_raw_and_zip_do_not_download_video_cover(self) -> None:
        for mode in (ParseMode.RAW, ParseMode.ZIP):
            with self.subTest(mode=mode):
                _, _, hybrid_pipeline = await self.run_until_pipeline(
                    use_flyinglife=True,
                    mode=mode,
                    video_cover=True,
                )
                self.assertFalse(hybrid_pipeline.call_args.kwargs["download_video_cover"])

    async def test_disabled_flyinglife_uses_parsehub_raw_url(self) -> None:
        raw_url = "https://www.douyin.com/video/123"
        _, parse_service, hybrid_pipeline = await self.run_until_pipeline(
            use_flyinglife=False,
            raw_url=raw_url,
        )

        parse_service.get_raw_url.assert_awaited_once()
        self.assertEqual(hybrid_pipeline.call_args.args[1], raw_url)


if __name__ == "__main__":
    unittest.main()
