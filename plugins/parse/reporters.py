import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from easy_ai18n import PreLocaleSelector
from pyrogram import Client, raw
from pyrogram.errors import BadRequest, FloodWait, Forbidden, MessageNotModified, SlowmodeWait
from pyrogram.types import LinkPreviewOptions, Message

from core import bs
from db import get_session
from log import logger
from plugins.context import get_config_target
from plugins.helpers import format_label
from plugins.parse.sender import MessageSender
from repo.settings import SettingsConfig
from services import SettingsService, StatusReporter
from services.guest_rich_message import delete_inline_guest_message, edit_inline_rich_message

logger = logger.bind(name="ParseReporter")

INLINE_ERROR_DETAIL_SECONDS = 15
EXPIRED_GUEST_MESSAGE_TEXT = "\u2800"


async def disable_progress_on_report_forbidden(msg: Message, config: SettingsConfig) -> None:
    """状态消息无权限时自动关闭解析进度。"""
    config.noprogress = True
    target = get_config_target(msg, include_member=False)
    async with get_session() as session:
        await SettingsService(session).patch_config(target=target, noprogress=True)
    logger.warning(f"已自动关闭解析进度: {target}")


class MessageStatusReporter(StatusReporter):
    """基于 Telegram Message 的状态报告器"""

    def __init__(
        self,
        cli: Client,
        user_msg: Message,
        *,
        t: PreLocaleSelector,
        config: SettingsConfig,
        on_forbidden: Callable[[Message, SettingsConfig], Awaitable[None]] | None = None,
    ):
        self._cli = cli
        self._user_msg = user_msg
        self._msg: Message | None = None
        self._t = t
        self._config = config
        self._on_forbidden = on_forbidden

    async def report(self, text: str) -> None:
        if self._config.noprogress:
            return
        await self._edit_text(format_label(text))

    async def report_error(self, stage: str, error: Exception) -> None:
        if self._config.hide_error:
            return

        t = format_label(self._t(f"{stage}错误:"))
        text = self._t(f"{t} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        if self._config.keep_error_log:
            return

        async def fn() -> None:
            await asyncio.sleep(15)
            await self.dismiss()

        loop = asyncio.get_running_loop()
        loop.create_task(fn())

    async def dismiss(self) -> None:
        if self._msg:
            await self._msg.delete()

    async def _edit_text(self, text: str, **kwargs: Any) -> None:
        try:
            if self._msg is None:
                self._msg = await MessageSender(self._cli, self._user_msg, self._config).text(text, **kwargs)
            else:
                if self._msg.text != text:
                    await self._msg.edit_text(text, **kwargs)
        except (FloodWait, SlowmodeWait, MessageNotModified):
            pass
        except Forbidden as e:
            logger.warning(f"状态消息发送失败, Bot 无权限: {e}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)


class InlineStatusReporter(StatusReporter):
    """基于 inline_message_id 的状态报告器"""

    def __init__(
        self,
        cli: Client,
        inline_message_id: str,
        caption: str = "",
        *,
        t: PreLocaleSelector,
        user_config: SettingsConfig,
        failure_text: str | None = None,
        rich: bool = False,
        guest_chat_id: int | str | None = None,
        error_auto_delete_after: float | None = None,
    ):
        self._cli = cli
        self._mid = inline_message_id
        self._caption = caption
        self._last_text: str | None = None
        self._t = t
        self._user_config = user_config
        self._failure_text = failure_text
        self._rich = rich
        self._guest_chat_id = guest_chat_id
        self._error_auto_delete_after = error_auto_delete_after
        self._error_cleanup_task: asyncio.Task[None] | None = None

    async def report(self, text: str) -> None:
        text = format_label(text)
        full = f"{self._caption}\n{text}" if self._caption else text
        if full == self._last_text:
            return
        self._last_text = full
        await self._edit_inline_text(inline_message_id=self._mid, text=full)

    async def report_error(self, stage: str, error: Exception) -> None:
        if self._user_config.hide_error:
            if self._failure_text:
                await self._edit_inline_text(
                    inline_message_id=self._mid,
                    text=self._failure_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            self._schedule_error_auto_delete()
            return

        text = self._t(f"{format_label(f'{stage}错误:')} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_inline_text(
            inline_message_id=self._mid, text=text, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

        self._schedule_error_auto_delete()
        if self._user_config.keep_error_log:
            return

        async def fn() -> None:
            await asyncio.sleep(INLINE_ERROR_DETAIL_SECONDS)
            final_text = self._caption or self._failure_text
            if not final_text:
                return
            await self._edit_inline_text(
                inline_message_id=self._mid,
                text=final_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

        loop = asyncio.get_running_loop()
        loop.create_task(fn())

    def _schedule_error_auto_delete(self) -> None:
        guest_chat_id = self._guest_chat_id
        auto_delete_after = self._error_auto_delete_after
        if guest_chat_id is None or auto_delete_after is None:
            return
        if self._error_cleanup_task is not None and not self._error_cleanup_task.done():
            return

        async def fn() -> None:
            await asyncio.sleep(auto_delete_after)
            deleted = False
            try:
                deleted = await delete_inline_guest_message(self._cli, self._mid, guest_chat_id)
            except Exception as e:
                logger.warning(f"Guest 错误消息自动删除失败: {type(e).__name__}: {e}")

            if deleted:
                return

            # Legacy inline IDs do not expose a safe chat message ID. Keep only
            # a blank braille character instead of leaving the error in the group.
            logger.warning("Guest 错误消息缺少可安全删除的 message_id，降级为空白占位")
            try:
                await self._edit_inline_text(
                    inline_message_id=self._mid,
                    text=EXPIRED_GUEST_MESSAGE_TEXT,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as e:
                logger.warning(f"Guest 错误消息清空失败: {type(e).__name__}: {e}")

        self._error_cleanup_task = asyncio.get_running_loop().create_task(fn())

    async def _edit_inline_text(self, **kwargs: Any) -> None:
        try:
            if self._rich:
                await edit_inline_rich_message(
                    self._cli,
                    kwargs.get("inline_message_id", self._mid),
                    raw.types.InputRichMessageMarkdown(markdown=kwargs["text"]),
                )
            else:
                await self._cli.edit_inline_text(**kwargs)
        except BadRequest as e:
            if not self._rich or "RICH_MESSAGE_BLOCK_UNSUPPORTED" not in str(e):
                raise
            self._rich = False
            logger.warning("Rich 进度消息不受支持，后续状态降级为普通文本")
            await self._cli.edit_inline_text(**kwargs)
        except (FloodWait, SlowmodeWait, MessageNotModified):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")

    async def dismiss(self) -> None:
        pass
