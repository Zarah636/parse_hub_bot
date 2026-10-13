import asyncio
import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from parsehub.types import VideoParseResult, VideoRef  # noqa: E402
from pyrogram import raw  # noqa: E402
from pyrogram.file_id import FileId, FileType  # noqa: E402
from pyrogram.types import InputMediaVideo, InputRichBlock, InputRichBlockVideo, InputRichMessage  # noqa: E402
from sqlalchemy import create_engine, insert, select, text  # noqa: E402

from alembic import command  # noqa: E402
from core import bs  # noqa: E402
from db.base import Base  # noqa: E402
from db.models.cache import Cache  # noqa: E402
from db.models.settings import Settings  # noqa: E402
from db.models.user import User  # noqa: E402
from plugins.flyinglife_control import flyinglife_callback  # noqa: E402
from plugins.parse.sender import MessageSender, PartialRichSend, send_cached, send_rich_media  # noqa: E402
from repo.settings import SettingsConfig  # noqa: E402
from services import tasks  # noqa: E402
from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult, PersistentCache  # noqa: E402
from services.flyinglife import FlyingLifeService  # noqa: E402
from services.media import prepare_video_thumbnail  # noqa: E402
from utils.helpers import run_cmd  # noqa: E402


class MigrationTests(unittest.TestCase):
    def test_private_and_upstream_heads_converge_without_losing_data(self) -> None:
        config = Config("alembic.ini")
        config.attributes["skip_logging_config"] = True
        self.assertEqual(ScriptDirectory.from_config(config).get_heads(), ["ae26d014c220"])
        for old_revision in (None, "c4f62d91a7e8", "765f26cfd84a"):
            with self.subTest(old_revision=old_revision):
                engine = create_engine("sqlite://")
                with engine.begin() as connection:
                    Base.metadata.create_all(connection)
                    config.attributes["connection"] = connection
                    if old_revision:
                        command.upgrade(config, old_revision)
                    connection.execute(insert(User).values(id=1, telegram_user_id=123))
                    connection.execute(
                        insert(Settings).values(
                            scope="user", user_id=1, config={"video_cover": False, "custom_content": True}
                        )
                    )
                    for key, url in (
                        ("valid", "https://example.com/post"),
                        ("invalid", "https://www.xiaohongshu.com/login"),
                    ):
                        connection.execute(
                            insert(Cache).values(
                                key=key,
                                url=url,
                                entry_json={"parse_result": {"title": "keep"}},
                                accessed_at=datetime.now(UTC),
                            )
                        )
                    command.upgrade(config, "head")
                    command.upgrade(config, "head")  # A restart must be idempotent.
                    self.assertEqual(
                        connection.execute(text("select version_num from alembic_version")).scalars().all(),
                        ["ae26d014c220"],
                    )
                    self.assertEqual(connection.execute(select(User.telegram_user_id)).scalar_one(), 123)
                    self.assertEqual(
                        connection.execute(select(Settings.config)).scalar_one(),
                        {"video_cover": False, "custom_content": True},
                    )
                    # An upstream database already at its cleanup head will not rerun those cleanups.
                    keys = connection.execute(select(Cache.key)).scalars().all()
                    self.assertIn("valid", keys)
                    if old_revision != "765f26cfd84a":
                        self.assertNotIn("invalid", keys)
                engine.dispose()


class TaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tasks._active.clear()
        tasks._pending_urls.clear()
        tasks.start()

    async def asyncTearDown(self) -> None:
        with patch.object(bs, "parse_shutdown_timeout", 0):
            await tasks.shutdown()
        tasks.start()

    async def test_global_limit_applies_to_queued_tasks(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        running = 0
        peak = 0

        @tasks.heavy_request
        async def worker() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if running == 2:
                started.set()
            try:
                await release.wait()
            finally:
                running -= 1

        with patch.object(tasks, "_slots", asyncio.Semaphore(2)):
            workers = [asyncio.create_task(worker()) for _ in range(5)]
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(running, 2)
            release.set()
            await asyncio.gather(*workers)
        self.assertEqual(peak, 2)
        self.assertFalse(tasks._active)

    async def test_shutdown_cancels_waiters_and_releases_url_claim(self) -> None:
        entered = asyncio.Event()

        @tasks.heavy_request
        async def worker() -> None:
            entered.set()
            await asyncio.Event().wait()

        async def request() -> None:
            with tasks.claim_url("url") as owned:
                self.assertTrue(owned)
                await worker()

        task = asyncio.create_task(request())
        await entered.wait()
        with tasks.claim_url("url") as owned:
            self.assertFalse(owned)
        with patch.object(bs, "parse_shutdown_timeout", 0):
            await tasks.shutdown()
        self.assertTrue(task.cancelled())
        self.assertFalse(tasks._pending_urls)
        self.assertFalse(tasks._active)

    async def test_spawn_stays_tracked_after_heavy_body_returns(self) -> None:
        outside_heavy = asyncio.Event()

        @tasks.heavy_request
        async def body() -> None:
            pass

        async def request() -> None:
            await body()
            outside_heavy.set()
            await asyncio.Event().wait()

        tasks.spawn(request())
        await outside_heavy.wait()
        self.assertEqual(len(tasks._active), 1)
        with patch.object(bs, "parse_shutdown_timeout", 0):
            await tasks.shutdown()
        self.assertFalse(tasks._active)

    async def test_cancelled_conversion_terminates_subprocess(self) -> None:
        entered = asyncio.Event()
        proc = MagicMock(returncode=None)

        async def communicate() -> tuple[bytes, bytes]:
            entered.set()
            await asyncio.Event().wait()
            return b"", b""

        proc.communicate = communicate
        proc.wait = AsyncMock()
        with patch("utils.helpers.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(run_cmd("ffmpeg"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


class RichSenderTests(unittest.IsolatedAsyncioTestCase):
    def sender(self) -> MessageSender:
        return cast(MessageSender, SimpleNamespace(upload_photo=AsyncMock(), rich_message=AsyncMock()))

    async def test_partial_upload_does_not_resend_or_return_cache(self) -> None:
        sender = self.sender()
        cast(Any, sender).rich_message.side_effect = [MagicMock(), RuntimeError("upload failed")]
        blocks: list[InputRichBlock] = [InputRichBlockVideo(InputMediaVideo("video.mp4")) for _ in range(51)]
        with patch("plugins.parse.sender.cache_media_from_rich_message", return_value=[]):
            with self.assertRaises(PartialRichSend):
                await send_rich_media(sender, blocks, [], slideshow=True)
        self.assertEqual(cast(Any, sender).rich_message.await_count, 2)

    async def test_first_send_timeout_is_not_retried_as_ordinary_media(self) -> None:
        sender = self.sender()
        cast(Any, sender).rich_message.side_effect = TimeoutError("unknown delivery")
        with self.assertRaises(PartialRichSend):
            await send_rich_media(sender, [InputRichBlockVideo(InputMediaVideo("video.mp4"))], [], slideshow=True)
        self.assertEqual(cast(Any, sender).rich_message.await_count, 1)

    async def test_returned_animation_type_matches_encoded_file_id(self) -> None:
        sender = self.sender()
        file_id = FileId(
            file_type=FileType.ANIMATION, dc_id=2, media_id=1, access_hash=2, file_reference=b"ref"
        ).encode()
        returned = CacheMedia(type=CacheMediaType.ANIMATION, file_id=file_id)
        with patch("plugins.parse.sender.cache_media_from_rich_message", return_value=[returned]):
            media = await send_rich_media(
                sender, [InputRichBlockVideo(InputMediaVideo("video.mp4"))], [], slideshow=True
            )
        assert media is not None
        self.assertEqual(media[0].type, CacheMediaType.ANIMATION)
        self.assertEqual(FileId.decode(media[0].file_id).file_type, FileType.ANIMATION)

    async def test_cached_partial_send_does_not_trigger_fallback(self) -> None:
        sender = cast(MessageSender, SimpleNamespace(config=SettingsConfig(rich_media_mode=True)))
        entry = CacheEntry(
            parse_result=CacheParseResult(),
            media=[CacheMedia(type=CacheMediaType.PHOTO, file_id="photo")],
            rich_media=True,
        )
        with (
            patch("plugins.parse.sender.send_rich_media", AsyncMock(side_effect=PartialRichSend("partial"))),
            patch("plugins.parse.sender.send_cached_single", AsyncMock()) as fallback,
        ):
            with self.assertRaises(PartialRichSend):
                await send_cached(sender, entry, "https://example.com/post")
        fallback.assert_not_awaited()

    async def test_kurigram_highlevel_rich_message_write_is_async(self) -> None:
        cli = MagicMock()
        cli.parser.parse = AsyncMock(return_value={"message": "body", "entities": []})
        from pyrogram.types import InputRichBlockParagraph

        result = await InputRichMessage(blocks=[InputRichBlockParagraph(cast(Any, "body"))]).write(client=cli)
        self.assertIsInstance(result, raw.types.InputRichMessage)


class FlyingLifeIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_timeout_includes_semaphore_wait(self) -> None:
        service = FlyingLifeService()
        service._semaphore = asyncio.Semaphore(0)
        with (
            patch.object(bs, "flyinglife_inline_parse_timeout", 0.01),
            patch.object(service, "_fetch_payload", AsyncMock()) as fetch,
        ):
            with self.assertRaises(TimeoutError):
                await service.parse_inline_candidate("https://example.com", "https://example.com")
        fetch.assert_not_awaited()

    async def test_download_does_not_mutate_cached_candidate(self) -> None:
        service = FlyingLifeService()
        candidate = VideoParseResult(
            video=VideoRef(url="https://example.com/video", thumb_url="https://example.com/cover")
        )

        async def mutate(result: Any, *args: Any, **kwargs: Any) -> Any:
            result.media.thumb_url = "local-cover.jpg"
            return SimpleNamespace(media=[], output_dir=None)

        with patch.object(service, "download", AsyncMock(side_effect=mutate)):
            await service.run(
                "url",
                "url",
                AsyncMock(),
                skip_media_processing=True,
                download_video_cover=True,
                save_metadata=False,
                _t=cast(Any, lambda s: s),
                prepared_result=candidate,
            )
        self.assertEqual(candidate.media.thumb_url, "https://example.com/cover")

    async def test_incomplete_entry_never_overwrites_persistent_cache(self) -> None:
        with patch("services.cache.get_session") as session:
            await PersistentCache().set("url", CacheEntry(parse_result=CacheParseResult(), complete=False))
        session.assert_not_called()

    async def test_cover_failure_falls_back_to_frame_thumbnail(self) -> None:
        with (
            patch("services.media.fetch_thumb", AsyncMock(return_value=None)),
            patch("services.media.create_video_thumbnail", AsyncMock(return_value=None)) as frame,
        ):
            await prepare_video_thumbnail("video.mp4", 10, cover="cover.webp")
        frame.assert_awaited_once_with("video.mp4", 10)

    async def test_non_admin_cannot_change_global_engine(self) -> None:
        query = MagicMock(data="flctl|set|0", message=MagicMock(), from_user=SimpleNamespace(id=123))
        query.answer = AsyncMock()
        with (
            patch.object(bs, "admin_ids", [456]),
            patch("plugins.flyinglife_control.flyinglife_runtime.set_preferred", AsyncMock()) as switch,
        ):
            await flyinglife_callback(MagicMock(), query)
        switch.assert_not_awaited()
        query.answer.assert_awaited_once()
