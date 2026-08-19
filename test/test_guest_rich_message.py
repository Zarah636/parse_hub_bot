import os
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram import Client, raw, utils  # noqa: E402
from pyrogram.file_id import FileType  # noqa: E402

from services.guest_rich_message import (  # noqa: E402
    PreparedRichMedia,
    RichLayout,
    RichMediaKind,
    RichMediaSource,
    RichMessageUnsupported,
    _prepare_media,
    _validate_media,
    assemble_rich_message,
    choose_layout,
    delete_inline_guest_message,
)
from services import CacheMedia, CacheMediaType  # noqa: E402


def photo(width: int = 1200, height: int = 1200) -> RichMediaSource:
    return RichMediaSource(RichMediaKind.PHOTO, width=width, height=height)


class GuestRichMessageTests(unittest.TestCase):
    def test_guest_compatible_layout_policy(self) -> None:
        self.assertEqual(choose_layout([photo()]), RichLayout.SINGLE)
        self.assertEqual(choose_layout([photo(), photo()]), RichLayout.COLLAGE)
        self.assertEqual(choose_layout([photo() for _ in range(10)]), RichLayout.COLLAGE)
        self.assertEqual(choose_layout([photo(800, 2400), photo()]), RichLayout.COLLAGE)
        self.assertEqual(
            choose_layout([photo(), RichMediaSource(RichMediaKind.VIDEO)]),
            RichLayout.SLIDESHOW,
        )
        self.assertEqual(
            choose_layout([photo(), RichMediaSource(RichMediaKind.PHOTO, is_live=True)]),
            RichLayout.SLIDESHOW,
        )

    def test_collage_message_places_body_below_media_without_separate_title(self) -> None:
        sources = [photo(), photo()]
        prepared = [
            PreparedRichMedia(
                RichMediaKind.PHOTO,
                raw.types.InputPhoto(id=index, access_hash=index + 10, file_reference=b"ref"),
            )
            for index in (1, 2)
        ]

        result = assemble_rich_message(
            title="Title",
            content="Description",
            source_url="https://example.com/source",
            media_sources=sources,
            prepared=prepared,
        )

        message = cast(raw.types.InputRichMessage, result.message)
        self.assertEqual(result.layout, RichLayout.COLLAGE)
        self.assertEqual(result.media_count, 2)
        self.assertFalse(any(isinstance(block, raw.types.PageBlockHeading1) for block in message.blocks))
        self.assertFalse(any(isinstance(block, raw.types.PageBlockTitle) for block in message.blocks))
        self.assertTrue(any(isinstance(block, raw.types.PageBlockParagraph) for block in message.blocks))
        collage = next(block for block in message.blocks if isinstance(block, raw.types.PageBlockCollage))
        self.assertEqual(sum(isinstance(block, raw.types.PageBlockPhoto) for block in collage.items), 2)
        self.assertTrue(any(isinstance(block, raw.types.PageBlockFooter) for block in message.blocks))
        paragraph_index = next(
            index for index, block in enumerate(message.blocks) if isinstance(block, raw.types.PageBlockParagraph)
        )
        self.assertLess(message.blocks.index(collage), paragraph_index)
        self.assertTrue(message.write())

    def test_text_article_keeps_title_above_body(self) -> None:
        result = assemble_rich_message(
            title="文章标题",
            content="文章正文",
            source_url="https://example.com/source",
            media_sources=[],
            prepared=[],
        )

        message = cast(raw.types.InputRichMessage, result.message)
        self.assertIsInstance(message.blocks[0], raw.types.PageBlockHeading1)
        self.assertIsInstance(message.blocks[1], raw.types.PageBlockParagraph)

    def test_rich_message_omits_semantically_duplicate_title(self) -> None:
        result = assemble_rich_message(
            title="闪亮登场玩元英 女团舞",
            content="闪亮登场！ #玩元英 #女团舞",
            source_url="https://example.com/source",
            media_sources=[],
            prepared=[],
        )

        message = cast(raw.types.InputRichMessage, result.message)
        self.assertFalse(any(isinstance(block, raw.types.PageBlockHeading1) for block in message.blocks))
        self.assertTrue(any(isinstance(block, raw.types.PageBlockParagraph) for block in message.blocks))

    def test_real_world_numeric_title_is_omitted_and_body_follows_collage(self) -> None:
        sources = [photo(), photo()]
        prepared = [
            PreparedRichMedia(
                RichMediaKind.PHOTO,
                raw.types.InputPhoto(id=index, access_hash=index + 10, file_reference=b"ref"),
            )
            for index in (1, 2)
        ]
        content = "湿夏.#夏日溯溪 #水中情绪片"

        result = assemble_rich_message(
            title="21123_湿夏夏日溯溪 水中情绪片",
            content=content,
            source_url="https://example.com/source",
            media_sources=sources,
            prepared=prepared,
        )

        message = cast(raw.types.InputRichMessage, result.message)
        self.assertIsInstance(message.blocks[0], raw.types.PageBlockCollage)
        self.assertFalse(any(isinstance(block, raw.types.PageBlockHeading1) for block in message.blocks))
        paragraph = next(block for block in message.blocks if isinstance(block, raw.types.PageBlockParagraph))
        self.assertIsInstance(paragraph.text, raw.types.TextPlain)
        paragraph_text = cast(raw.types.TextPlain, paragraph.text)
        self.assertEqual(paragraph_text.text, content)

    def test_mixed_media_serializes_as_slideshow(self) -> None:
        sources = [photo(), RichMediaSource(RichMediaKind.VIDEO, width=1920, height=1080)]
        prepared = [
            PreparedRichMedia(
                RichMediaKind.PHOTO,
                raw.types.InputPhoto(id=1, access_hash=11, file_reference=b"photo"),
            ),
            PreparedRichMedia(
                RichMediaKind.VIDEO,
                raw.types.InputDocument(id=2, access_hash=12, file_reference=b"video"),
            ),
        ]

        result = assemble_rich_message(
            title="Mixed",
            content="Photo and video",
            source_url="https://example.com/source",
            media_sources=sources,
            prepared=prepared,
        )

        message = cast(raw.types.InputRichMessage, result.message)
        self.assertEqual(result.layout, RichLayout.SLIDESHOW)
        self.assertTrue(any(isinstance(block, raw.types.PageBlockSlideshow) for block in message.blocks))
        self.assertTrue(message.write())

    def test_rich_build_carries_original_file_id_cache_entries(self) -> None:
        cache_media = CacheMedia(type=CacheMediaType.PHOTO, file_id="photo-file-id")
        source = photo()
        prepared = PreparedRichMedia(
            RichMediaKind.PHOTO,
            raw.types.InputPhoto(id=1, access_hash=11, file_reference=b"photo"),
            cache_media,
        )

        result = assemble_rich_message(
            title="Title",
            content="Body",
            source_url="https://example.com/source",
            media_sources=[source],
            prepared=[prepared],
        )

        self.assertEqual(result.cache_media, [cache_media])

    def test_more_than_fifty_media_is_rejected(self) -> None:
        with self.assertRaises(RichMessageUnsupported):
            _validate_media([photo() for _ in range(51)])

    def test_kurigram_guest_and_raw_rich_contract_is_available(self) -> None:
        self.assertTrue(hasattr(Client, "on_guest_message"))
        self.assertTrue(hasattr(Client, "answer_guest_query"))
        self.assertTrue(hasattr(raw.types, "InputRichMessage"))
        self.assertTrue(hasattr(raw.types, "PageBlockCollage"))
        self.assertTrue(hasattr(raw.types, "PageBlockSlideshow"))
        self.assertTrue(hasattr(raw.functions.messages, "EditInlineBotMessage"))


class GuestRichMessageUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_message_id64_can_be_deleted_from_its_chat(self) -> None:
        cli = MagicMock()
        cli.delete_messages = AsyncMock(return_value=1)
        inline_message_id = utils.pack_inline_message_id(
            raw.types.InputBotInlineMessageID64(
                dc_id=2,
                owner_id=123,
                id=456,
                access_hash=789,
            )
        )

        deleted = await delete_inline_guest_message(cli, inline_message_id, -123)

        self.assertTrue(deleted)
        cli.delete_messages.assert_awaited_once_with(-123, 456)

    async def test_guest_message_is_not_deleted_when_owner_does_not_match_chat(self) -> None:
        cli = MagicMock()
        cli.delete_messages = AsyncMock(return_value=1)
        inline_message_id = utils.pack_inline_message_id(
            raw.types.InputBotInlineMessageID64(
                dc_id=2,
                owner_id=999,
                id=456,
                access_hash=789,
            )
        )

        deleted = await delete_inline_guest_message(cli, inline_message_id, -123)

        self.assertFalse(deleted)
        cli.delete_messages.assert_not_awaited()

    async def test_legacy_guest_inline_id_is_not_guessed_for_deletion(self) -> None:
        cli = MagicMock()
        cli.delete_messages = AsyncMock(return_value=1)
        inline_message_id = utils.pack_inline_message_id(
            raw.types.InputBotInlineMessageID(
                dc_id=2,
                id=456,
                access_hash=789,
            )
        )

        deleted = await delete_inline_guest_message(cli, inline_message_id, -123)

        self.assertFalse(deleted)
        cli.delete_messages.assert_not_awaited()

    async def test_uploaded_photo_produces_reusable_cache_file_id(self) -> None:
        cli = MagicMock()
        cli.save_file = AsyncMock(return_value=MagicMock())
        uploaded_photo = raw.types.Photo(
            id=101,
            access_hash=202,
            file_reference=b"photo-ref",
            date=0,
            sizes=[raw.types.PhotoSize(type="x", w=1200, h=800, size=100)],
            dc_id=2,
        )
        cli.invoke = AsyncMock(return_value=raw.types.MessageMediaPhoto(photo=uploaded_photo))

        prepared = await _prepare_media(
            cli,
            RichMediaSource(RichMediaKind.PHOTO, path=Path("photo.jpg"), width=1200, height=800),
        )

        assert prepared.cache_media is not None
        self.assertEqual(prepared.cache_media.type, CacheMediaType.PHOTO)
        cached = utils.get_input_media_from_file_id(prepared.cache_media.file_id, FileType.PHOTO)
        self.assertIsInstance(cached, raw.types.InputMediaPhoto)

    async def test_uploaded_video_produces_reusable_cache_file_id(self) -> None:
        cli = MagicMock()
        cli.save_file = AsyncMock(return_value=MagicMock())
        uploaded_document = raw.types.Document(
            id=303,
            access_hash=404,
            file_reference=b"video-ref",
            date=0,
            mime_type="video/mp4",
            size=1024,
            dc_id=2,
            attributes=[],
        )
        cli.invoke = AsyncMock(return_value=raw.types.MessageMediaDocument(document=uploaded_document))

        prepared = await _prepare_media(
            cli,
            RichMediaSource(
                RichMediaKind.VIDEO,
                path=Path("video.mp4"),
                width=1920,
                height=1080,
                duration=10,
            ),
        )

        assert prepared.cache_media is not None
        self.assertEqual(prepared.cache_media.type, CacheMediaType.VIDEO)
        cached = utils.get_input_media_from_file_id(prepared.cache_media.file_id, FileType.VIDEO)
        self.assertIsInstance(cached, raw.types.InputMediaDocument)


if __name__ == "__main__":
    unittest.main()
