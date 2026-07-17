import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from parsehub.types import ImageParseResult, VideoParseResult, VideoRef  # noqa: E402

from services.flyinglife import (  # noqa: E402
    FLYINGLIFE_USER_AGENT,
    FlyingLifeDownloadError,
    FlyingLifeParseError,
    FlyingLifeService,
)


class FlyingLifeParseTests(unittest.IsolatedAsyncioTestCase):
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
        service._api_request = AsyncMock(return_value={"musics": ["https://example.com/audio.mp3"]})  # type: ignore[method-assign]

        with self.assertRaisesRegex(FlyingLifeParseError, "暂不支持音频"):
            await service.parse("https://example.com/post", "https://example.com/post")

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


if __name__ == "__main__":
    unittest.main()
