import os
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from easy_ai18n import PreLocaleSelector  # noqa: E402
from parsehub.types import VideoParseResult, VideoRef  # noqa: E402

from services.flyinglife import FlyingLifeRunResult, FlyingLifeUnavailable, flyinglife  # noqa: E402
from services.hybrid import HybridParsePipeline  # noqa: E402
from services.pipeline import PipelineResult, StatusReporter  # noqa: E402


class Reporter:
    def __init__(self) -> None:
        self.report = AsyncMock()
        self.report_error = AsyncMock()
        self.dismiss = AsyncMock()


def translate(text: str) -> str:
    return text


def build_pipeline(
    reporter: Reporter,
    *,
    flyinglife_parse_result: VideoParseResult | None = None,
) -> HybridParsePipeline:
    return HybridParsePipeline(
        "https://v.douyin.com/example/",
        "https://v.douyin.com/example/",
        cast(StatusReporter, reporter),
        platform_id="douyin",
        singleflight=False,
        flyinglife_parse_result=flyinglife_parse_result,
        _t=cast(PreLocaleSelector, translate),
    )


class HybridParsePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_failure_falls_back_before_returning(self) -> None:
        reporter = Reporter()
        pipeline = build_pipeline(reporter)
        parse_result = VideoParseResult(video=VideoRef(url="https://example.com/local.mp4"))
        local_result = PipelineResult(parse_result=parse_result)

        with (
            patch.object(flyinglife, "can_attempt", return_value=True),
            patch.object(flyinglife, "run", AsyncMock(side_effect=FlyingLifeUnavailable("代理失败"))),
            patch.object(pipeline, "_run_local", AsyncMock(return_value=local_result)) as run_local,
        ):
            result = await pipeline.run()

        self.assertIs(result, local_result)
        run_local.assert_awaited_once_with(singleflight=False)
        reporter.report.assert_not_awaited()

    async def test_remote_success_is_marked_for_cache_policy(self) -> None:
        reporter = Reporter()
        pipeline = build_pipeline(reporter)
        parse_result = VideoParseResult(video=VideoRef(url="https://example.com/proxy.mp4"))
        remote_result = FlyingLifeRunResult(parse_result=parse_result)

        with (
            patch.object(flyinglife, "can_attempt", return_value=True),
            patch.object(flyinglife, "run", AsyncMock(return_value=remote_result)),
        ):
            result = await pipeline.run()

        assert result is not None
        self.assertEqual(result.engine, "flyinglife")

    async def test_remote_download_reuses_prepared_parse_result(self) -> None:
        reporter = Reporter()
        parse_result = VideoParseResult(video=VideoRef(url="https://example.com/proxy.mp4"))
        pipeline = build_pipeline(reporter, flyinglife_parse_result=parse_result)
        remote_result = FlyingLifeRunResult(parse_result=parse_result)

        with (
            patch.object(flyinglife, "can_attempt", return_value=True),
            patch.object(flyinglife, "run", AsyncMock(return_value=remote_result)) as run,
        ):
            await pipeline.run()

        assert run.await_args is not None
        self.assertIs(run.await_args.kwargs["prepared_result"], parse_result)


if __name__ == "__main__":
    unittest.main()
