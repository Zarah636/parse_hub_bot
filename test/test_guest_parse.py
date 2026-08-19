import os
import unittest
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram.types import Message  # noqa: E402

from plugins.guest_parse import extract_guest_url, guest_parse  # noqa: E402
from plugins.helpers import COMMANDS, build_start_text  # noqa: E402
from repo.settings import SettingsConfig  # noqa: E402
from services import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult  # noqa: E402


class FakeSessionContext(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None


class GuestParseTests(unittest.TestCase):
    def test_extracts_supported_url_only_from_replied_message(self) -> None:
        parser = MagicMock()
        parser.get_platform.side_effect = lambda value: object() if value.startswith("https://example.com/") else None
        service = MagicMock()
        service.parser = parser
        message = cast(
            Message,
            SimpleNamespace(
                text="@bot https://ignored.example",
                reply_to_message=SimpleNamespace(text="look https://example.com/post", caption=None),
            ),
        )

        with patch("plugins.guest_parse.ParseService", return_value=service):
            self.assertEqual(extract_guest_url(message), "https://example.com/post")

    def test_no_reply_produces_no_url(self) -> None:
        message = cast(Message, SimpleNamespace(reply_to_message=None))
        self.assertIsNone(extract_guest_url(message))

    def test_flyinglife_command_is_in_menu_and_help(self) -> None:
        self.assertIn("flyinglife", COMMANDS)
        self.assertIn("/flyinglife", build_start_text()["zh-hans"])
        self.assertIn("Send share link", build_start_text()["en-us"])
        self.assertIn("/flyinglife", build_start_text()["en-us"])


class GuestParseAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def _run_cached_guest(self, *, use_flyinglife: bool) -> tuple[MagicMock, MagicMock, MagicMock]:
        url = "https://example.com/post"
        raw_url = "https://example.com/canonical"
        parser = MagicMock()
        parser.get_platform.return_value = object()
        parse_service = MagicMock()
        parse_service.parser = parser
        parse_service.get_platform.return_value = SimpleNamespace(id="example")
        parse_service.get_raw_url = AsyncMock(return_value=raw_url)
        user_service = MagicMock()
        user_service.get_lang = AsyncMock(return_value="test")
        settings_service = MagicMock()
        settings_service.get_config_by_user = AsyncMock(return_value=SettingsConfig())
        cli = MagicMock()
        cli.answer_guest_query = AsyncMock(return_value=SimpleNamespace(inline_message_id="inline-id"))
        cli.edit_inline_text = AsyncMock()
        message = cast(
            Message,
            SimpleNamespace(
                guest_query_id="123",
                from_user=SimpleNamespace(id=456),
                reply_to_message=SimpleNamespace(text=url, caption=None),
            ),
        )
        cached = CacheEntry(parse_result=CacheParseResult(title="Cached", content="Body"))
        edit_rich = AsyncMock(return_value=True)

        with (
            patch("plugins.guest_parse.get_session", return_value=FakeSessionContext()),
            patch("plugins.guest_parse.UserService", return_value=user_service),
            patch("plugins.guest_parse.SettingsService", return_value=settings_service),
            patch("plugins.guest_parse.ParseService", return_value=parse_service),
            patch("plugins.guest_parse.flyinglife.should_attempt", return_value=use_flyinglife),
            patch("plugins.guest_parse.persistent_cache.get", AsyncMock(return_value=cached)) as cache_get,
            patch("plugins.guest_parse.edit_inline_rich_message", edit_rich),
            patch("plugins.guest_parse.t_", {"test": lambda text: text}),
        ):
            await guest_parse(cli, message)

        cache_get.assert_awaited_once_with(raw_url)
        cli.answer_guest_query.assert_awaited_once()
        edit_rich.assert_awaited_once()
        return parse_service, cli, edit_rich

    async def test_guest_enabled_uses_original_canonical_cache_identity(self) -> None:
        parse_service, _, _ = await self._run_cached_guest(use_flyinglife=True)
        parse_service.get_raw_url.assert_awaited_once_with("https://example.com/post")

    async def test_guest_disabled_resolves_url_with_parsehub_only(self) -> None:
        parse_service, _, _ = await self._run_cached_guest(use_flyinglife=False)
        parse_service.get_raw_url.assert_awaited_once_with("https://example.com/post")

    async def test_guest_success_uses_singleflight_and_writes_file_id_cache(self) -> None:
        url = "https://example.com/post"
        raw_url = "https://example.com/canonical"
        parse_service = MagicMock()
        parse_service.parser.get_platform.return_value = object()
        parse_service.get_platform.return_value = SimpleNamespace(id="example")
        parse_service.get_raw_url = AsyncMock(return_value=raw_url)
        user_service = MagicMock()
        user_service.get_lang = AsyncMock(return_value="test")
        settings_service = MagicMock()
        settings_service.get_config_by_user = AsyncMock(return_value=SettingsConfig())
        cli = MagicMock()
        cli.answer_guest_query = AsyncMock(return_value=SimpleNamespace(inline_message_id="inline-id"))
        cli.edit_inline_text = AsyncMock()
        message = cast(
            Message,
            SimpleNamespace(
                guest_query_id="123",
                from_user=SimpleNamespace(id=456),
                reply_to_message=SimpleNamespace(text=url, caption=None),
            ),
        )
        parse_result = SimpleNamespace(title="Title", content="Body", raw_url=raw_url)
        pipeline_result = SimpleNamespace(engine="flyinglife", parse_result=parse_result)
        pipeline = MagicMock()
        pipeline.__enter__.return_value = pipeline
        pipeline.run = AsyncMock(return_value=pipeline_result)
        pipeline.waited = False
        cache_media = [CacheMedia(type=CacheMediaType.VIDEO, file_id="video-file-id")]
        rich = SimpleNamespace(message=object(), layout="single", media_count=1, cache_media=cache_media)

        with (
            patch("plugins.guest_parse.get_session", return_value=FakeSessionContext()),
            patch("plugins.guest_parse.UserService", return_value=user_service),
            patch("plugins.guest_parse.SettingsService", return_value=settings_service),
            patch("plugins.guest_parse.ParseService", return_value=parse_service),
            patch("plugins.guest_parse.flyinglife.should_attempt", return_value=True),
            patch("plugins.guest_parse.persistent_cache.get", AsyncMock(return_value=None)),
            patch("plugins.guest_parse.persistent_cache.set", AsyncMock()) as cache_set,
            patch("plugins.guest_parse.parse_cache.get", AsyncMock(return_value=None)),
            patch("plugins.guest_parse.HybridParsePipeline", return_value=pipeline) as pipeline_cls,
            patch("plugins.guest_parse.build_pipeline_rich_message", AsyncMock(return_value=rich)),
            patch("plugins.guest_parse.edit_inline_rich_message", AsyncMock(return_value=True)),
            patch("plugins.guest_parse.t_", {"test": lambda text: text}),
        ):
            await guest_parse(cli, message)

        self.assertTrue(pipeline_cls.call_args.kwargs["singleflight"])
        cache_set.assert_awaited_once()
        assert cache_set.await_args is not None
        cache_key, cache_entry = cache_set.await_args.args
        self.assertEqual(cache_key, raw_url)
        self.assertEqual(cache_entry.media, cache_media)


if __name__ == "__main__":
    unittest.main()
