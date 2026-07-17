import os
import unittest
from typing import cast
from unittest.mock import AsyncMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from easy_ai18n import PreLocaleSelector  # noqa: E402
from pyrogram.types import Message  # noqa: E402

from plugins.parse import MessageStatusReporter  # noqa: E402
from repo.user_settings import UserConfig  # noqa: E402


def translate(text: str) -> str:
    return text


class MessageStatusReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_status_is_not_sent_twice(self) -> None:
        user_message = AsyncMock()
        status_message = AsyncMock()
        user_message.reply_text.return_value = status_message
        reporter = MessageStatusReporter(
            cast(Message, user_message),
            _t=cast(PreLocaleSelector, translate),
            user_config=UserConfig(),
        )

        await reporter.report("解 析 中...")
        await reporter.report("解 析 中...")

        user_message.reply_text.assert_awaited_once()
        status_message.edit_text.assert_not_awaited()

    async def test_progress_change_edits_existing_message(self) -> None:
        user_message = AsyncMock()
        status_message = AsyncMock()
        user_message.reply_text.return_value = status_message
        reporter = MessageStatusReporter(
            cast(Message, user_message),
            _t=cast(PreLocaleSelector, translate),
            user_config=UserConfig(),
        )

        await reporter.report("解 析 中...")
        await reporter.report("下 载 中...")

        status_message.edit_text.assert_awaited_once_with("**▎下 载 中...**")


if __name__ == "__main__":
    unittest.main()
