import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from pyrogram import raw  # noqa: E402
from pyrogram.errors import BadRequest, MessageNotModified  # noqa: E402

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

    async def test_rich_inline_status_keeps_rich_message_shape(self) -> None:
        cli = MagicMock()
        reporter = InlineStatusReporter(
            cli,
            "inline-id",
            t=lambda text: text,  # type: ignore[arg-type]
            user_config=SettingsConfig(),
            rich=True,
        )

        with patch(
            "plugins.parse.reporters.edit_inline_rich_message",
            AsyncMock(return_value=True),
        ) as edit_rich:
            await reporter.report("解 析 中...")

        edit_rich.assert_awaited_once()
        assert edit_rich.await_args is not None
        message = edit_rich.await_args.args[2]
        self.assertIsInstance(message, raw.types.InputRichMessageMarkdown)
        self.assertIn("解 析 中", message.markdown)

    async def test_unsupported_rich_status_falls_back_to_text(self) -> None:
        cli = MagicMock()
        cli.edit_inline_text = AsyncMock()
        reporter = InlineStatusReporter(
            cli,
            "inline-id",
            t=lambda text: text,  # type: ignore[arg-type]
            user_config=SettingsConfig(),
            rich=True,
        )
        error = BadRequest(
            value="[400 RICH_MESSAGE_BLOCK_UNSUPPORTED]",
            rpc_name="messages.EditInlineBotMessage",
        )

        with patch(
            "plugins.parse.reporters.edit_inline_rich_message",
            AsyncMock(side_effect=error),
        ):
            await reporter.report("解 析 中...")

        cli.edit_inline_text.assert_awaited_once()
        self.assertFalse(reporter._rich)

    async def test_guest_hidden_error_is_deleted_after_configured_delay(self) -> None:
        cli = MagicMock()
        cli.edit_inline_text = AsyncMock()
        reporter = InlineStatusReporter(
            cli,
            "inline-id",
            t=lambda text: text,  # type: ignore[arg-type]
            user_config=SettingsConfig(hide_error=True),
            failure_text="解析失败，请重新尝试。",
            guest_chat_id=-123,
            error_auto_delete_after=60,
        )

        with (
            patch("plugins.parse.reporters.asyncio.sleep", AsyncMock()) as sleep,
            patch(
                "plugins.parse.reporters.delete_inline_guest_message",
                AsyncMock(return_value=True),
            ) as delete_guest,
        ):
            await reporter.report_error("解析", RuntimeError("boom"))
            assert reporter._error_cleanup_task is not None
            await reporter._error_cleanup_task

        sleep.assert_awaited_once_with(60)
        delete_guest.assert_awaited_once_with(cli, "inline-id", -123)
        cli.edit_inline_text.assert_awaited_once()

    async def test_guest_error_cleanup_uses_minimal_fallback_when_delete_is_unavailable(self) -> None:
        cli = MagicMock()
        cli.edit_inline_text = AsyncMock()
        reporter = InlineStatusReporter(
            cli,
            "legacy-inline-id",
            t=lambda text: text,  # type: ignore[arg-type]
            user_config=SettingsConfig(hide_error=True),
            failure_text="解析失败，请重新尝试。",
            guest_chat_id=-123,
            error_auto_delete_after=60,
        )

        with (
            patch("plugins.parse.reporters.asyncio.sleep", AsyncMock()),
            patch(
                "plugins.parse.reporters.delete_inline_guest_message",
                AsyncMock(return_value=False),
            ),
        ):
            await reporter.report_error("解析", RuntimeError("boom"))
            assert reporter._error_cleanup_task is not None
            await reporter._error_cleanup_task

        self.assertEqual(cli.edit_inline_text.await_count, 2)
        self.assertEqual(cli.edit_inline_text.await_args.kwargs["text"], "\u2800")


if __name__ == "__main__":
    unittest.main()
