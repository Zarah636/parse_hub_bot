import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from parsehub.types import ImageParseResult, VideoParseResult, VideoRef  # noqa: E402

from core import bs  # noqa: E402
from services.flyinglife import (  # noqa: E402
    FLYINGLIFE_USER_AGENT,
    FlyingLifeDownloadError,
    FlyingLifeParseError,
    FlyingLifeService,
)


class FlyingLifeParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_only_does_not_start_proxy_download(self) -> None:
        service = FlyingLifeService()
        reporter = AsyncMock()
        parse_result = VideoParseResult(video=VideoRef(url="https://example.com/video.mp4"))

        with (
            patch.object(service, "parse", AsyncMock(return_value=parse_result)) as parse,
            patch.object(service, "download", AsyncMock()) as download,
        ):
            result = await service.parse_only(
                "https://v.douyin.com/example/",
                "https://v.douyin.com/example/",
                reporter,
                _t=lambda text: text,
            )

        self.assertIs(result, parse_result)
        parse.assert_awaited_once()
        download.assert_not_awaited()

    async def test_title_is_preserved(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "title": "Example title",
                "text": "Example content",
                "videos": ["https://example.com/video.mp4"],
            }
        )

        result = await service.parse("https://v.douyin.com/example/", "https://www.douyin.com/video/1")

        self.assertIsInstance(result, VideoParseResult)
        self.assertEqual(result.title, "Example title")

    async def test_douyin_video_uses_proxy_and_keeps_cover_private(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "text": "示例文案",
                "images": ["https://p3-sign.douyinpic.com/cover.webp?x-signature=secret"],
                "videos": ["https://v5-default.365yg.com/video.mp4?token=secret"],
            }
        )

        result = await service.parse("https://v.douyin.com/example/", "https://www.douyin.com/video/1")

        self.assertIsInstance(result, VideoParseResult)
        self.assertEqual(result.content, "示例文案")
        self.assertIsInstance(result.media, VideoRef)
        self.assertIn("/api/media_proxy.php?", result.media.url)
        self.assertIn("type=video", result.media.url)
        self.assertIn("type=image", result.media.thumb_url or "")

    async def test_inline_candidate_separates_public_preview_and_private_download(self) -> None:
        service = FlyingLifeService()
        api_request = AsyncMock(
            return_value={
                "title": "Inline title",
                "text": "Inline content",
                "images": ["https://p11-sign.douyinpic.com/cover.webp?x-signature=preview"],
                "videos": ["https://v5-default.365yg.com/video.mp4?token=download"],
            }
        )
        service._api_request = api_request  # type: ignore[method-assign]

        candidate = await service.parse_inline_candidate(
            "https://v.douyin.com/example/", "https://www.douyin.com/video/1"
        )

        self.assertIsInstance(candidate.preview_result, VideoParseResult)
        self.assertIsInstance(candidate.download_result, VideoParseResult)
        preview = candidate.preview_result.media
        download = candidate.download_result.media
        self.assertIsInstance(preview, VideoRef)
        self.assertIsInstance(download, VideoRef)
        self.assertEqual(preview.thumb_url, "https://p11-sign.douyinpic.com/cover.webp?x-signature=preview")
        self.assertEqual(preview.url, "https://v5-default.365yg.com/video.mp4?token=download")
        self.assertIn("/api/media_proxy.php?", download.thumb_url or "")
        self.assertIn("type=image", download.thumb_url or "")
        self.assertIn("/api/media_proxy.php?", download.url)
        self.assertIn("type=video", download.url)
        api_request.assert_awaited_once_with(
            "/api/parse.php",
            json_body={"url": "https://v.douyin.com/example/"},
            timeout=bs.flyinglife_inline_parse_timeout,
        )

    async def test_douyin_gallery_preserves_image_order(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "text": "图集文案",
                "images": ["https://example.com/1.webp", "https://example.com/2.webp"],
            }
        )

        result = await service.parse("https://www.iesdouyin.com/share/note/1/", "https://www.douyin.com/note/1")

        self.assertIsInstance(result, ImageParseResult)
        self.assertEqual(result.content, "图集文案")
        self.assertEqual(len(result.media or []), 2)
        self.assertIn("1.webp", (result.media or [])[0].url)
        self.assertIn("2.webp", (result.media or [])[1].url)

    async def test_audio_result_falls_back(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={"musics": ["https://example.com/audio.mp3"]}
        )

        with self.assertRaisesRegex(FlyingLifeParseError, "暂不支持音频"):
            await service.parse("https://example.com/post", "https://example.com/post")

    async def test_null_music_collection_is_normalized(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "text": "视频文案",
                "videos": ["https://example.com/video.mp4"],
                "musics": None,
            }
        )

        result = await service.parse("https://v.douyin.com/example/", "https://www.douyin.com/video/1")

        self.assertIsInstance(result, VideoParseResult)

    async def test_null_text_is_normalized(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "text": None,
                "video": "https://example.com/video.mp4",
            }
        )

        result = await service.parse("https://v.douyin.com/example/", "https://www.douyin.com/video/1")

        self.assertIsInstance(result, VideoParseResult)
        self.assertEqual(result.content, "")

    async def test_empty_result_falls_back(self) -> None:
        service = FlyingLifeService()
        service._api_request = AsyncMock(return_value={"text": "只有文字"})  # type: ignore[method-assign]

        with self.assertRaisesRegex(FlyingLifeParseError, "没有解析到可用媒体"):
            await service.parse("https://example.com/post", "https://example.com/post")

    async def test_proxy_redirect_is_rejected_without_following_cdn(self) -> None:
        requests: list[httpx.Request] = []

        def redirect(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/video.mp4"})

        service = FlyingLifeService()
        service._session_id = "private-session"
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(redirect),
            headers={"User-Agent": FLYINGLIFE_USER_AGENT},
        )
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir) / "video.mp4"
                with self.assertRaisesRegex(FlyingLifeDownloadError, "HTTP 302"):
                    await service._download_proxy("https://parse.flyinglife.cn/api/media_proxy.php", target, "video")
        finally:
            await service.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["X-Session-Id"], "private-session")
        self.assertEqual(requests[0].headers["User-Agent"], FLYINGLIFE_USER_AGENT)

    async def test_disabled_video_cover_is_not_downloaded(self) -> None:
        service = FlyingLifeService()
        parse_result = VideoParseResult(
            video=VideoRef(url="https://example.com/video.mp4", thumb_url="https://example.com/cover.jpg")
        )
        parse_result.raw_url = "https://www.douyin.com/video/1"

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("services.flyinglife.bs.download_dir", Path(temp_dir)),
            patch("services.flyinglife.VideoFile", side_effect=lambda path: MagicMock(path=path)),
            patch.object(service, "_download_proxy", AsyncMock()) as download_proxy,
        ):
            result = await service.download(parse_result, download_video_cover=False)

        download_proxy.assert_awaited_once()
        assert download_proxy.await_args is not None
        self.assertEqual(download_proxy.await_args.args[2], "video")
        self.assertEqual(result.media.path.name, "001.mp4")

    async def test_video_cover_failure_does_not_abort_video_download(self) -> None:
        service = FlyingLifeService()
        video_ref = VideoRef(url="https://example.com/video.mp4", thumb_url="https://example.com/cover.jpg")
        parse_result = VideoParseResult(video=video_ref)
        parse_result.raw_url = "https://www.douyin.com/video/1"

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("services.flyinglife.bs.download_dir", Path(temp_dir)),
            patch("services.flyinglife.VideoFile", side_effect=lambda path: MagicMock(path=path)),
            patch.object(
                service,
                "_download_proxy",
                AsyncMock(side_effect=[FlyingLifeDownloadError("cover failed"), None]),
            ) as download_proxy,
        ):
            result = await service.download(parse_result, download_video_cover=True)

        self.assertEqual(download_proxy.await_count, 2)
        self.assertIsNone(video_ref.thumb_url)
        self.assertEqual(result.media.path.name, "001.mp4")


if __name__ == "__main__":
    unittest.main()
