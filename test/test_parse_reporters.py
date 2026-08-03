import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram.errors import MessageNotModified  # noqa: E402

from plugins.parse.reporters import MessageStatusReporter  # noqa: E402


class MessageStatusReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_telegram_edit_does_not_abort_pipeline(self) -> None:
        reporter = MessageStatusReporter(
            MagicMock(),
            MagicMock(),
            t=lambda text: text,
            config=MagicMock(noprogress=False),
        )
        status_message = MagicMock(text="stale text")
        status_message.edit_text = AsyncMock(side_effect=MessageNotModified())
        reporter._msg = status_message

        await reporter.report("解 析 中...")

        status_message.edit_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
