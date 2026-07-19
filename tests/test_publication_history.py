import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram import Client, enums  # noqa: E402
from pyrogram.enums import ChatType  # noqa: E402
from pyrogram.types import CallbackQuery, Chat, ForumTopic, Message, MessageEntity, User  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from db.base import Base  # noqa: E402
from db.models.media_publication import MediaPublication  # noqa: E402
from plugins.parse import (  # noqa: E402
    _prompt_for_duplicate,
    _refresh_publication_record,
    _resolve_result_source_url,
    duplicate_media_callback,
)
from repo.media_publication import MediaPublicationRepo  # noqa: E402
from repo.user_settings import UserConfig  # noqa: E402
from services.cache import CacheParseResult  # noqa: E402
from services.publication_history import (  # noqa: E402
    DuplicateConfirmationStore,
    PendingDuplicateConfirmation,
    PublicationRecord,
    douyin_content_id,
    make_content_key,
    message_scope,
    normalize_source_url,
    safe_message_link,
)
from tools.backfill_publication_sources import source_url_from_message  # noqa: E402


def build_message(*, chat_type: ChatType, thread_id: int | None = None) -> Message:
    chat = Chat(id=-1001234567890, type=chat_type, title="测试群组")
    topic = ForumTopic(id=thread_id, title="下载区") if thread_id else None
    return Message(
        id=100,
        chat=chat,
        from_user=User(id=42, is_bot=False, first_name="Tester"),
        message_thread_id=thread_id,
        topic=topic,
    )


class PublicationIdentityTests(unittest.TestCase):
    def test_douyin_content_id_uses_stable_source_identifier(self) -> None:
        self.assertEqual(
            douyin_content_id("https://www.douyin.com/video/7639803196995913914"),
            "7639803196995913914",
        )
        self.assertIsNone(douyin_content_id("https://v.douyin.com/geHpwnqsDoE/"))

    def test_old_cache_entries_remain_compatible(self) -> None:
        entry = CacheParseResult.model_validate({"title": "title", "content": "content"})

        self.assertIsNone(entry.raw_url)

    def test_backfill_reads_stable_source_link_from_telegram_caption(self) -> None:
        canonical_url = "https://www.douyin.com/video/7639803196995913914"
        message = Message(
            id=1,
            caption=cast(Any, "Source"),
            caption_entities=[
                MessageEntity(
                    type=enums.MessageEntityType.TEXT_LINK,
                    offset=0,
                    length=6,
                    url=canonical_url,
                )
            ],
        )

        self.assertEqual(source_url_from_message(message), canonical_url)

    def test_douyin_content_id_matches_across_canonical_hosts(self) -> None:
        first = "https://www.douyin.com/video/7652298364587187023?previous_page=web_code_link"
        second = "https://www.iesdouyin.com/share/video/7652298364587187023/"

        self.assertEqual(make_content_key(first), make_content_key(second))

    def test_short_url_normalization_ignores_fragment_and_trailing_slash(self) -> None:
        first = "HTTPS://V.DOUYIN.COM/example/#fragment"
        second = "https://v.douyin.com/example"

        self.assertEqual(normalize_source_url(first), normalize_source_url(second))
        self.assertEqual(make_content_key(first), make_content_key(second))

    def test_topic_is_part_of_scope(self) -> None:
        topic_message = build_message(chat_type=ChatType.SUPERGROUP, thread_id=88)
        forum_message = build_message(chat_type=ChatType.FORUM, thread_id=89)
        general_message = build_message(chat_type=ChatType.SUPERGROUP)
        private_message = build_message(chat_type=ChatType.PRIVATE)

        self.assertEqual(message_scope(topic_message), (-1001234567890, 88))
        self.assertEqual(message_scope(forum_message), (-1001234567890, 89))
        self.assertEqual(message_scope(general_message), (-1001234567890, 0))
        self.assertIsNone(message_scope(private_message))

    def test_supergroup_link_points_to_topic_message(self) -> None:
        message = build_message(chat_type=ChatType.SUPERGROUP, thread_id=88)

        self.assertEqual(safe_message_link(message), "https://t.me/c/1234567890/88/100")

    def test_forum_link_points_to_topic_message(self) -> None:
        message = build_message(chat_type=ChatType.FORUM, thread_id=88)

        self.assertEqual(safe_message_link(message), "https://t.me/c/1234567890/88/100")


class MediaPublicationRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_upsert_tracks_latest_topic_across_the_same_group(self) -> None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            repo = MediaPublicationRepo(session)
            await repo.upsert(
                content_key="a" * 64,
                source_url="https://example.com/video/1",
                chat_id=-1001,
                message_thread_id=10,
                topic_title="话题 A",
                message_id=101,
                message_link="https://t.me/c/1/10/101",
                requester_user_id=42,
                published_at=now,
            )

        async with self.sessions.begin() as session:
            repo = MediaPublicationRepo(session)
            await repo.upsert(
                content_key="a" * 64,
                source_url="https://example.com/video/1",
                chat_id=-1001,
                message_thread_id=11,
                topic_title="话题 B",
                message_id=102,
                message_link="https://t.me/c/1/11/102",
                requester_user_id=42,
                published_at=now,
            )

        async with self.sessions() as session:
            repo = MediaPublicationRepo(session)
            publication = await repo.get(content_key="a" * 64, chat_id=-1001)
            other_group = await repo.get(content_key="a" * 64, chat_id=-1002)

        assert publication is not None
        self.assertEqual(publication.message_id, 102)
        self.assertEqual(publication.message_thread_id, 11)
        self.assertEqual(publication.topic_title, "话题 B")
        self.assertIsNone(other_group)

    async def test_remove_deletes_stale_publication(self) -> None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            repo = MediaPublicationRepo(session)
            await repo.upsert(
                content_key="b" * 64,
                source_url="https://example.com/video/2",
                chat_id=-1001,
                message_thread_id=10,
                topic_title="music",
                message_id=201,
                message_link="https://t.me/c/1/10/201",
                requester_user_id=42,
                published_at=now,
            )

        async with self.sessions.begin() as session:
            removed = await MediaPublicationRepo(session).remove(content_key="b" * 64, chat_id=-1001)

        self.assertTrue(removed)
        async with self.sessions() as session:
            self.assertIsNone(await MediaPublicationRepo(session).get(content_key="b" * 64, chat_id=-1001))


class StableSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_douyin_url_resolves_to_stable_parse_source(self) -> None:
        short_url = "https://v.douyin.com/geHpwnqsDoE/"
        canonical_url = "https://www.douyin.com/video/7639803196995913914"
        parse_result = SimpleNamespace(raw_url=short_url)
        parse_service = SimpleNamespace(get_raw_url=AsyncMock(return_value=canonical_url))

        with patch("plugins.parse.ParseService", return_value=parse_service):
            source_url = await _resolve_result_source_url(
                short_url,
                short_url,
                "douyin",
                cast(Any, parse_result),
            )

        self.assertEqual(source_url, canonical_url)
        self.assertEqual(parse_result.raw_url, canonical_url)
        parse_service.get_raw_url.assert_awaited_once_with(short_url)


class DuplicateConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_store_is_single_use(self) -> None:
        store = DuplicateConfirmationStore()
        pending = PendingDuplicateConfirmation(
            url="https://example.com/video/1",
            mode="preview",
            user_id=42,
            chat_id=-1001,
            message_thread_id=10,
        )

        token = await store.create(pending)

        self.assertEqual(await store.get(token), pending)
        self.assertEqual(await store.pop(token), pending)
        self.assertIsNone(await store.pop(token))

    async def test_deleted_original_message_clears_history(self) -> None:
        record = PublicationRecord(
            source_url="https://example.com/video/1",
            chat_id=-1001234567890,
            message_thread_id=129,
            topic_title=None,
            message_id=99,
            message_link="https://t.me/c/1234567890/129/99",
            requester_user_id=42,
            published_at=datetime(2026, 7, 17, 16, 0, tzinfo=UTC),
        )
        client = SimpleNamespace(get_messages=AsyncMock(return_value=None))

        with patch("plugins.parse.publication_history.remove", AsyncMock(return_value=True)) as remove:
            refreshed = await _refresh_publication_record(cast(Client, client), record)

        self.assertIsNone(refreshed)
        remove.assert_awaited_once_with(record.source_url, record.chat_id)

    async def test_live_topic_name_replaces_numeric_fallback(self) -> None:
        record = PublicationRecord(
            source_url="https://example.com/video/1",
            chat_id=-1001234567890,
            message_thread_id=129,
            topic_title=None,
            message_id=99,
            message_link=None,
            requester_user_id=42,
            published_at=datetime(2026, 7, 17, 16, 0, tzinfo=UTC),
        )
        original = Message(
            id=99,
            chat=Chat(id=record.chat_id, type=ChatType.FORUM, title="测试群组"),
            message_thread_id=129,
            topic=ForumTopic(id=129, title="old name"),
        )
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=original),
            get_forum_topics_by_id=AsyncMock(return_value=ForumTopic(id=129, title="music")),
        )

        refreshed = await _refresh_publication_record(cast(Client, client), record)

        assert refreshed is not None
        self.assertEqual(refreshed.topic_title, "music")
        self.assertEqual(refreshed.message_link, "https://t.me/c/1234567890/129/99")

    async def test_cross_topic_prompt_names_previous_topic(self) -> None:
        message = build_message(chat_type=ChatType.FORUM, thread_id=88)
        message.reply_text = AsyncMock()  # type: ignore[method-assign]
        record = PublicationRecord(
            source_url="https://example.com/video/1",
            chat_id=-1001234567890,
            message_thread_id=77,
            topic_title="下载区",
            message_id=99,
            message_link="https://t.me/c/1234567890/88/99",
            requester_user_id=42,
            published_at=datetime(2026, 7, 17, 16, 0, tzinfo=UTC),
        )

        with (
            patch("plugins.parse._refresh_publication_record", AsyncMock(return_value=record)),
            patch("plugins.parse.duplicate_confirmations.create", AsyncMock(return_value="token")),
        ):
            prompted = await _prompt_for_duplicate(
                cast(Client, AsyncMock()), cast(Message, message), record.source_url, "preview", record
            )

        self.assertTrue(prompted)
        call = message.reply_text.await_args
        assert call is not None
        kwargs = call.kwargs
        self.assertEqual(kwargs["parse_mode"], enums.ParseMode.HTML)
        self.assertEqual(
            call.args[0],
            '此视频已在话题「<a href="https://t.me/c/1234567890/88/99">下载区</a>」发送过，是否重新分享？',
        )
        self.assertNotIn("时间", call.args[0])
        self.assertNotIn("消息 ID", call.args[0])
        self.assertTrue(kwargs["link_preview_options"].is_disabled)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["重新分享", "取消"])
        self.assertTrue(all(button.url is None for button in buttons))
        self.assertTrue(any("confirm:token" in (button.callback_data or "") for button in buttons))

    async def test_same_topic_prompt_does_not_repeat_topic_name(self) -> None:
        message = build_message(chat_type=ChatType.FORUM, thread_id=88)
        message.reply_text = AsyncMock()  # type: ignore[method-assign]
        record = PublicationRecord(
            source_url="https://example.com/video/1",
            chat_id=-1001234567890,
            message_thread_id=88,
            topic_title="下载区",
            message_id=99,
            message_link="https://t.me/c/1234567890/88/99",
            requester_user_id=42,
            published_at=datetime(2026, 7, 17, 16, 0, tzinfo=UTC),
        )

        with (
            patch("plugins.parse._refresh_publication_record", AsyncMock(return_value=record)),
            patch("plugins.parse.duplicate_confirmations.create", AsyncMock(return_value="token")),
        ):
            await _prompt_for_duplicate(
                cast(Client, AsyncMock()), cast(Message, message), record.source_url, "preview", record
            )

        call = message.reply_text.await_args
        assert call is not None
        self.assertEqual(
            call.args[0],
            '此视频已在<a href="https://t.me/c/1234567890/88/99">本话题</a>发送过，是否重新分享？',
        )
        self.assertNotIn("下载区", call.args[0])

    async def test_plain_group_prompt_has_no_location_details(self) -> None:
        message = build_message(chat_type=ChatType.SUPERGROUP)
        message.reply_text = AsyncMock()  # type: ignore[method-assign]
        record = PublicationRecord(
            source_url="https://example.com/video/1",
            chat_id=-1001234567890,
            message_thread_id=0,
            topic_title=None,
            message_id=99,
            message_link="https://t.me/c/1234567890/99",
            requester_user_id=42,
            published_at=datetime(2026, 7, 17, 16, 0, tzinfo=UTC),
        )

        with (
            patch("plugins.parse._refresh_publication_record", AsyncMock(return_value=record)),
            patch("plugins.parse.duplicate_confirmations.create", AsyncMock(return_value="token")),
        ):
            await _prompt_for_duplicate(
                cast(Client, AsyncMock()), cast(Message, message), record.source_url, "preview", record
            )

        call = message.reply_text.await_args
        assert call is not None
        self.assertEqual(
            call.args[0],
            '此视频已在<a href="https://t.me/c/1234567890/99">本群</a>发送过，是否重新分享？',
        )

    async def test_other_user_cannot_consume_confirmation(self) -> None:
        pending = PendingDuplicateConfirmation(
            url="https://example.com/video/1",
            mode="preview",
            user_id=42,
            chat_id=-1001234567890,
            message_thread_id=88,
        )
        message = build_message(chat_type=ChatType.SUPERGROUP, thread_id=88)
        query = SimpleNamespace(
            data="duplicate_media:confirm:token",
            message=message,
            from_user=SimpleNamespace(id=99),
            answer=AsyncMock(),
        )

        with (
            patch("plugins.parse.duplicate_confirmations.get", AsyncMock(return_value=pending)),
            patch("plugins.parse.duplicate_confirmations.pop", AsyncMock()) as pop_confirmation,
        ):
            await duplicate_media_callback(cast(Client, AsyncMock()), cast(CallbackQuery, query))

        pop_confirmation.assert_not_awaited()
        query.answer.assert_awaited_once_with("这不是你的操作", show_alert=True)

    async def test_confirm_forces_real_redownload(self) -> None:
        pending = PendingDuplicateConfirmation(
            url="https://example.com/video/1",
            mode="preview",
            user_id=42,
            chat_id=-1001234567890,
            message_thread_id=88,
        )
        message = build_message(chat_type=ChatType.SUPERGROUP, thread_id=88)
        message.edit_text = AsyncMock()  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="duplicate_media:confirm:token",
            message=message,
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        account = SimpleNamespace(lang=None, config=UserConfig())
        account_service = SimpleNamespace(ensure_account=AsyncMock(return_value=account))

        @asynccontextmanager
        async def fake_get_session() -> AsyncIterator[object]:
            yield object()

        with (
            patch("plugins.parse.duplicate_confirmations.get", AsyncMock(return_value=pending)),
            patch("plugins.parse.duplicate_confirmations.pop", AsyncMock(return_value=pending)),
            patch("plugins.parse.get_session", fake_get_session),
            patch("plugins.parse.AccountService", return_value=account_service),
            patch("plugins.parse._handle_parse_request", AsyncMock()) as handle_request,
        ):
            await duplicate_media_callback(cast(Client, AsyncMock()), cast(CallbackQuery, query))

        query.answer.assert_awaited_once_with("已确认，正在重新分享")
        handle_request.assert_awaited_once()
        call = handle_request.await_args
        assert call is not None
        kwargs = call.kwargs
        self.assertTrue(kwargs["bypass_cache"])
        self.assertTrue(kwargs["force_reupload"])
        self.assertEqual(kwargs["requester_user_id"], 42)


if __name__ == "__main__":
    unittest.main()
