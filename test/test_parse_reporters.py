import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram.errors import MessageNotModified  # noqa: E402

from plugins.parse.reporters import InlineStatusReporter, MessageStatusReporter  # noqa: E402
from repo.settings import SettingsConfig  # noqa: E402


class MessageStatusReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_telegram_edit_does_not_abort_pipeline(self) -> None:
        reporter = MessageStatusReporter(
            MagicMock(),
            MagicMock(),
            t=lambda text: text,  # type: ignore[arg-type]
            config=MagicMock(noprogress=False),
        )
        status_message = MagicMock(text="stale text")
        status_message.edit_text = AsyncMock(side_effect=MessageNotModified())
        reporter._msg = status_message

        await reporter.report("解 析 中...")

        status_message.edit_text.assert_awaited_once()

    async def test_inline_hidden_error_replaces_progress_with_failure_text(self) -> None:
        cli = MagicMock()
        cli.edit_inline_text = AsyncMock()
        config = SettingsConfig(hide_error=True)
        reporter = InlineStatusReporter(
            cli,
            "inline-id",
            t=lambda text: text,  # type: ignore[arg-type]
            user_config=config,
            failure_text="解析失败，请重新尝试。",
        )

        await reporter.report_error("解析", RuntimeError("boom"))

        cli.edit_inline_text.assert_awaited_once()
        self.assertEqual(cli.edit_inline_text.await_args.kwargs["text"], "解析失败，请重新尝试。")


if __name__ == "__main__":
    unittest.main()
