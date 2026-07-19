import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from html import escape
from itertools import batched
from typing import Any, BinaryIO, Literal, cast

from easy_ai18n import PreLocaleSelector
from parsehub.types import (
    AniFile,
    AniRef,
    AnyMediaRef,
    AnyParseResult,
    ImageFile,
    LivePhotoFile,
    PostType,
    VideoFile,
)
from pyrogram import Client, enums, filters
from pyrogram.enums import ButtonStyle
from pyrogram.errors import (
    FloodWait,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    MsgIdInvalid,
    SlowmodeWait,
    WebpageCurlFailed,
    WebpageMediaEmpty,
)
from pyrogram.types import (
    InlineKeyboardButton as Ikb,
)
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)
from pyrogram.types import (
    CallbackQuery,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

from core import bs
from db import get_session
from i18n import t_
from log import logger
from plugins.filters import forwarded_from_bot_filter, platform_filter, via_me_filter
from plugins.helpers import (
    ProcessedMedia,
    build_caption,
    build_caption_by_str,
    create_richtext_telegraph,
    resolve_media_info,
)
from repo.user_settings import UserConfig
from services import AccountService, ParseService
from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult, parse_cache, persistent_cache
from services.flyinglife import flyinglife
from services.hybrid import HybridParsePipeline
from services.pipeline import PipelineResult, StatusReporter
from services.publication_history import (
    PendingDuplicateConfirmation,
    PublicationRecord,
    douyin_content_id,
    duplicate_confirmations,
    message_scope,
    publication_history,
    safe_message_link,
    topic_title,
)
from utils.helpers import pack_dir_to_tar_gz, to_list, with_request_id
from utils.rate_limit import ParseRateLimitExceeded, parse_rate_limit

logger = logger.bind(name="Parse")
SKIP_DOWNLOAD_THRESHOLD = 0
GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD = 5
MAX_RETRIES = 5
DUPLICATE_CALLBACK_PREFIX = "duplicate_media"


@dataclass(slots=True)
class MediaSendResult:
    cache_entry: CacheEntry | None
    messages: list[Message]


def _media_input(media: str | BinaryIO | None) -> str | BinaryIO:
    return cast(str | BinaryIO, media)


async def _send_with_rate_limit[T](
    send_coro_fn: Callable[[], Awaitable[T]],
) -> T:
    """带自动重试的发送包装器。

    Args:
        send_coro_fn: 返回协程的可调用对象（lambda 或函数），每次重试会重新调用
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await send_coro_fn()
        except (FloodWait, SlowmodeWait) as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"{e.ID} 重试 ({attempt + 1}/{MAX_RETRIES})，等待 {e.value}s")
                await asyncio.sleep(e.value)
            else:
                raise
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")
    raise RuntimeError("发送重试失败")


class MessageStatusReporter(StatusReporter):
    """基于 Telegram Message 的状态报告器"""

    def __init__(self, user_msg: Message, *, _t: PreLocaleSelector, user_config: UserConfig):
        self._user_msg = user_msg
        self._msg: Message | None = None
        self._t = _t
        self._user_config = user_config
        self._last_text: str | None = None

    async def report(self, text: str) -> None:
        if self._user_config.noprogress:
            return
        await self._edit_text(f"**▎{text}**")

    async def report_error(self, stage: str, error: Exception) -> None:
        text = self._t(f"**▎{stage}错误:** \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n**问题反馈: @MisakaSisters**")
        await self._edit_text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        if self._user_config.keep_error_log:
            return

        async def fn() -> None:
            await asyncio.sleep(15)
            if self._msg:
                await self._msg.delete()

        loop = asyncio.get_running_loop()
        loop.create_task(fn())

    async def dismiss(self) -> None:
        if self._msg:
            await self._msg.delete()

    async def _edit_text(self, text: str, **kwargs: Any) -> None:
        if text == self._last_text:
            return
        try:
            if self._msg is None:
                self._msg = await self._user_msg.reply_text(text, **kwargs)
            else:
                await self._msg.edit_text(text, **kwargs)
            self._last_text = text
        except MessageNotModified:
            self._last_text = text
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")


def _linked_location(label: str, link: str | None) -> str:
    safe_label = escape(label)
    if not link:
        return safe_label
    return f'<a href="{escape(link, quote=True)}">{safe_label}</a>'


def _duplicate_confirmation_markup(token: str) -> Ikm:
    return Ikm(
        [
            [
                Ikb(
                    "重新分享",
                    callback_data=f"{DUPLICATE_CALLBACK_PREFIX}:confirm:{token}",
                    style=ButtonStyle.PRIMARY,
                ),
                Ikb(
                    "取消",
                    callback_data=f"{DUPLICATE_CALLBACK_PREFIX}:cancel:{token}",
                    style=ButtonStyle.DEFAULT,
                ),
            ]
        ]
    )


async def _refresh_publication_record(cli: Client, record: PublicationRecord) -> PublicationRecord | None:
    try:
        original = cast(
            Message | None,
            await cli.get_messages(record.chat_id, record.message_id, replies=0),
        )
    except (MessageIdInvalid, MsgIdInvalid):
        original = None
    except Exception as e:
        logger.warning(
            f"核验原视频消息失败，暂时保留历史记录: chat_id={record.chat_id}, "
            f"message_id={record.message_id}, error={type(e).__name__}: {e}"
        )
        return record

    if original is None or getattr(original, "empty", False):
        try:
            removed = await publication_history.remove(record.source_url, record.chat_id)
        except Exception as e:
            removed = False
            logger.warning(
                f"清理已删除视频的发布历史失败，后续成功发送将覆盖记录: chat_id={record.chat_id}, "
                f"message_id={record.message_id}, error={type(e).__name__}: {e}"
            )
        logger.info(
            f"原视频消息已删除，清理发布历史: chat_id={record.chat_id}, "
            f"message_id={record.message_id}, removed={removed}"
        )
        return None

    current_topic_title = topic_title(original)
    if record.message_thread_id:
        try:
            topic = await cli.get_forum_topics_by_id(record.chat_id, record.message_thread_id)
        except Exception as e:
            logger.debug(
                f"获取论坛话题名称失败，使用消息中的话题信息: chat_id={record.chat_id}, "
                f"thread_id={record.message_thread_id}, error={type(e).__name__}: {e}"
            )
        else:
            current_topic_title = getattr(topic, "title", None) or current_topic_title

    return replace(
        record,
        topic_title=current_topic_title or record.topic_title,
        message_link=safe_message_link(original) or record.message_link,
    )


async def _prompt_for_duplicate(
    cli: Client,
    msg: Message,
    url: str,
    mode: str,
    record: PublicationRecord,
) -> bool:
    live_record = await _refresh_publication_record(cli, record)
    if live_record is None:
        return False
    record = live_record
    scope = message_scope(msg)
    if scope is None or not msg.from_user:
        return False

    chat_id, message_thread_id = scope
    pending = PendingDuplicateConfirmation(
        url=url,
        mode=mode,
        user_id=msg.from_user.id,
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )
    token = await duplicate_confirmations.create(pending)
    if message_thread_id and record.message_thread_id == message_thread_id:
        location = _linked_location("本话题", record.message_link)
        text = f"此视频已在{location}发送过，是否重新分享？"
    elif record.message_thread_id:
        previous_topic = record.topic_title or f"话题 #{record.message_thread_id}"
        location = _linked_location(previous_topic, record.message_link)
        text = f"此视频已在话题「{location}」发送过，是否重新分享？"
    else:
        location = _linked_location("本群", record.message_link)
        text = f"此视频已在{location}发送过，是否重新分享？"
    await msg.reply_text(
        text,
        reply_markup=_duplicate_confirmation_markup(token),
        parse_mode=enums.ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    logger.info(
        f"命中重复视频: chat_id={chat_id}, thread_id={message_thread_id}, "
        f"message_id={record.message_id}, requester={msg.from_user.id}"
    )
    return True


def _cache_entry_has_video(entry: CacheEntry) -> bool:
    return any(media.type == CacheMediaType.VIDEO for media in entry.media or [])


def _cache_entry_source_url(entry: CacheEntry, fallback: str) -> str:
    return entry.parse_result.raw_url or fallback


async def _record_video_publication(
    source_url: str,
    request_message: Message,
    sent_messages: list[Message],
    *,
    requester_user_id: int | None,
) -> None:
    try:
        publication = await publication_history.record(
            source_url,
            request_message,
            sent_messages,
            requester_user_id=requester_user_id,
        )
    except Exception as e:
        logger.warning(f"记录视频发布历史失败: {type(e).__name__}: {e}")
        return
    if publication:
        logger.info(
            f"已记录视频发布: chat_id={publication.chat_id}, "
            f"thread_id={publication.message_thread_id}, message_id={publication.message_id}"
        )


async def _find_video_publication(source_url: str, message: Message) -> PublicationRecord | None:
    try:
        return await publication_history.find(source_url, message)
    except Exception as e:
        logger.warning(f"查询视频发布历史失败，继续解析: {type(e).__name__}: {e}")
        return None


async def _resolve_result_source_url(
    url: str,
    raw_url: str,
    platform_id: str,
    parse_result: AnyParseResult,
) -> str:
    source_url = str(parse_result.raw_url or raw_url)
    if platform_id.lower() == "douyin" and douyin_content_id(source_url) is None:
        try:
            resolved_url = await ParseService().get_raw_url(url)
        except Exception as e:
            logger.warning(f"抖音作品链接标准化失败，暂用原链接查重: {type(e).__name__}: {e}")
        else:
            if douyin_content_id(resolved_url):
                source_url = resolved_url

    parse_result.raw_url = source_url
    return source_url


# ── Handler ──────────────────────────────────────────────────────────


@Client.on_message(
    filters.command(["jx", "jxjx", "raw", "zip"])
    | ((filters.text | filters.caption) & ~via_me_filter & platform_filter(True) & ~forwarded_from_bot_filter)
)
async def jx(cli: Client, msg: Message) -> None:
    mode = "preview"
    bypass_cache = False
    lang = None
    user_config = UserConfig()

    if msg.from_user:
        async with get_session() as session:
            current = await AccountService(session, msg.from_user.id).ensure_account()
            user_config = current.config
            lang = current.lang
            mode = user_config.default_mode

    _t = t_[lang]

    if msg.command:
        match msg.command[0]:
            case "raw":
                mode = "raw"
            case "jx":
                mode = "preview"
            case "jxjx":
                mode = "preview"
                bypass_cache = True
            case "zip":
                mode = "zip"

        text = " ".join(msg.command[1:]) if msg.command[1:] else ""
        if not text and msg.reply_to_message:
            text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        if not text:
            await msg.reply_text(_t("**▎请加上链接或回复一条消息**"))
            return
    else:
        text = msg.text or msg.caption or ""

    tokens = text.strip().split()
    urls = list({i for i in tokens if ParseService().parser.get_platform(i)})[:10]

    if not urls:
        await msg.reply_text(_t("**▎不支持的平台**"))
        return

    tasks = [
        _handle_parse_request(
            cli,
            msg,
            url=url,
            mode=mode,
            delete_share_url_msg=user_config.auto_delete_url,
            bypass_cache=bypass_cache,
            _t=_t,
            user_config=user_config,
        )
        for url in urls
    ]
    await asyncio.gather(*tasks)


@Client.on_callback_query(filters.regex(rf"^{DUPLICATE_CALLBACK_PREFIX}:"))
async def duplicate_media_callback(cli: Client, cq: CallbackQuery) -> None:
    if not cq.data or not cq.message:
        return

    parts = str(cq.data).split(":", 2)
    if len(parts) != 3:
        await cq.answer("无效的确认请求", show_alert=True)
        return
    _, action, token = parts
    if action not in {"confirm", "cancel"}:
        await cq.answer("无效的确认请求", show_alert=True)
        return

    pending = await duplicate_confirmations.get(token)
    if pending is None:
        await cq.answer("确认已过期，请重新发送链接", show_alert=True)
        return
    if cq.from_user.id != pending.user_id:
        await cq.answer("这不是你的操作", show_alert=True)
        return
    if message_scope(cq.message) != (pending.chat_id, pending.message_thread_id):
        await cq.answer("确认请求与当前会话不匹配", show_alert=True)
        return

    pending = await duplicate_confirmations.pop(token)
    if pending is None:
        await cq.answer("该请求已被处理", show_alert=True)
        return

    if action == "cancel":
        await cq.answer("已取消")
        await cq.message.edit_text(
            "已取消。",
            reply_markup=None,
            parse_mode=enums.ParseMode.DISABLED,
        )
        logger.info(
            f"用户取消重复视频上传: chat_id={pending.chat_id}, "
            f"thread_id={pending.message_thread_id}, requester={pending.user_id}"
        )
        return

    await cq.answer("已确认，正在重新分享")
    await cq.message.edit_text(
        "正在重新分享……",
        reply_markup=None,
        parse_mode=enums.ParseMode.DISABLED,
    )
    async with get_session() as session:
        current = await AccountService(session, pending.user_id).ensure_account()

    logger.info(
        f"用户确认重复视频上传: chat_id={pending.chat_id}, "
        f"thread_id={pending.message_thread_id}, requester={pending.user_id}"
    )
    await _handle_parse_request(
        cli,
        cq.message,
        url=pending.url,
        mode=pending.mode,
        bypass_cache=True,
        force_reupload=True,
        requester_user_id=pending.user_id,
        _t=t_[current.lang],
        user_config=current.config,
    )


# ── 主流程 ───────────────────────────────────────────────────────────


def _get_parse_user_id(_: Client, msg: Message, **__: Any) -> int | None:
    return msg.chat.id if msg.chat else None


@with_request_id
async def _handle_parse_request(
    cli: Client,
    msg: Message,
    *,
    url: str,
    mode: Literal["raw", "preview", "zip"] | str = "preview",
    delete_share_url_msg: bool = False,
    bypass_cache: bool = False,
    force_reupload: bool = False,
    requester_user_id: int | None = None,
    _t: PreLocaleSelector,
    user_config: UserConfig,
) -> None:
    try:
        await handle_parse(
            cli,
            msg,
            url=url,
            mode=mode,
            delete_share_url_msg=delete_share_url_msg,
            bypass_cache=bypass_cache,
            force_reupload=force_reupload,
            requester_user_id=requester_user_id,
            _t=_t,
            user_config=user_config,
        )
    except ParseRateLimitExceeded as e:
        if e.should_notify:
            logger.warning(
                f"速率限制 {e.retry_after:.1f}s, chat_id={msg.chat.id if msg.chat else None}, msg_id={msg.id}"
            )
            text = _t(f"**▎解析过于频繁, 请在 {e.retry_after:.1f}s 后重试**")
            if bs.demo_mode:
                text += _t(
                    "\n\n>**为保障所有用户的使用体验, 当前已启用速率限制**\n\n"
                    ">本项目为开源项目, 如有高频或批量解析需求, 建议自行部署实例, "
                    "以免触发 Telegram API 全局速率限制\n\n"
                    "**开源地址: [GitHub](https://github.com/z-mio/parse_hub_bot)**"
                )
            msg = await msg.reply_text(text, link_preview_options=LinkPreviewOptions(is_disabled=True))

            async def fn(retry_after: float) -> None:
                await asyncio.sleep(retry_after)
                await msg.delete()

            loop = asyncio.get_running_loop()
            loop.create_task(fn(e.retry_after))


@parse_rate_limit(_get_parse_user_id)
async def handle_parse(
    cli: Client,
    msg: Message,
    *,
    url: str,
    mode: Literal["raw", "preview", "zip"] | str = "preview",
    delete_share_url_msg: bool = False,
    bypass_cache: bool = False,
    force_reupload: bool = False,
    requester_user_id: int | None = None,
    _t: PreLocaleSelector,
    user_config: UserConfig,
) -> None:
    chat_id = msg.chat.id if msg.chat else None
    requester_user_id = requester_user_id or (msg.from_user.id if msg.from_user else None)
    logger.info(f"收到解析请求: url={url}, chat_id={chat_id}, msg_id={msg.id}, mode={mode}")
    if bypass_cache:
        logger.debug("bypass_cache=True 绕过缓存")
    if delete_share_url_msg:
        logger.debug(f"自动删除分享链接消息: chat_id={chat_id}, msg_id: {msg.id}")
        try:
            await msg.delete()
        except Exception as e:
            logger.warning(f"删除分享链接消息失败: chat_id={chat_id}, msg_id: {msg.id}, error: {e}")

    reporter = MessageStatusReporter(msg, _t=_t, user_config=user_config)
    match mode:
        case "raw":
            use_caching = False
            skip_media_processing = True
            singleflight = False
            save_metadata = False
        case "zip":
            use_caching = False
            skip_media_processing = True
            singleflight = False
            save_metadata = True
        case _:
            use_caching = True
            skip_media_processing = False
            singleflight = not bypass_cache
            save_metadata = False
    try:
        platform_id = ParseService().get_platform(url).id
    except Exception as e:
        await reporter.report_error(_t("获取原始链接"), e)
        return

    use_flyinglife = flyinglife.can_attempt(platform_id)
    if use_flyinglife:
        # 不预先调用 ParseHub 还原短链，确保 FlyingLife 是第一条网络链路。
        raw_url = url
    else:
        try:
            raw_url = await ParseService().get_raw_url(url)
        except Exception as e:
            await reporter.report_error(_t("获取原始链接"), e)
            return

    if mode == "preview" and not force_reupload:
        publication = await _find_video_publication(raw_url, msg)
        if publication and await _prompt_for_duplicate(cli, msg, url, mode, publication):
            return

    if use_caching and not bypass_cache and (cached := await persistent_cache.get(raw_url)):
        logger.debug("file_id 缓存命中, 直接发送")
        cached_source_url = _cache_entry_source_url(cached, raw_url)
        if mode == "preview" and not force_reupload and _cache_entry_has_video(cached):
            publication = await _find_video_publication(cached_source_url, msg)
            if publication and await _prompt_for_duplicate(cli, msg, url, mode, publication):
                return
        sent_messages = await _send_cached(msg, cached, raw_url, user_config=user_config)
        if _cache_entry_has_video(cached):
            await _record_video_publication(
                cached_source_url,
                msg,
                sent_messages,
                requester_user_id=requester_user_id,
            )
        return

    flyinglife_parse_result: AnyParseResult | None = None
    skip_flyinglife = False
    if mode == "preview" and not force_reupload and use_flyinglife:
        try:
            flyinglife_parse_result = await flyinglife.parse_only(url, raw_url, reporter, _t=_t)
        except Exception as e:
            skip_flyinglife = True
            logger.warning(f"FlyingLife 预解析失败, fallback ParseHub: {type(e).__name__}: {e}")
        else:
            source_url = await _resolve_result_source_url(url, raw_url, platform_id, flyinglife_parse_result)
            if flyinglife_parse_result.type == PostType.VIDEO:
                publication = await _find_video_publication(source_url, msg)
                if publication and await _prompt_for_duplicate(cli, msg, url, mode, publication):
                    await reporter.dismiss()
                    return

    cached_parse_result = None if bypass_cache else await parse_cache.get(raw_url)
    with HybridParsePipeline(
        url,
        raw_url,
        reporter,
        parse_result=cached_parse_result,
        platform_id=platform_id,
        singleflight=singleflight,
        skip_media_processing=skip_media_processing,
        skip_download_threshold=SKIP_DOWNLOAD_THRESHOLD,
        gif_only_skip_download_count_threshold=GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD if mode == "preview" else 0,
        save_metadata=save_metadata,
        flyinglife_parse_result=flyinglife_parse_result,
        skip_flyinglife=skip_flyinglife,
        _t=_t,
    ) as pipeline:
        if (result := await pipeline.run()) is None:
            if pipeline.waited:
                logger.debug("Singleflight 等待完成, 重新检查缓存")
                if not bypass_cache and (cached := await persistent_cache.get(raw_url)):
                    cached_source_url = _cache_entry_source_url(cached, raw_url)
                    if mode == "preview" and not force_reupload and _cache_entry_has_video(cached):
                        publication = await _find_video_publication(cached_source_url, msg)
                        if publication and await _prompt_for_duplicate(cli, msg, url, mode, publication):
                            return
                    sent_messages = await _send_cached(msg, cached, raw_url, user_config=user_config)
                    if _cache_entry_has_video(cached):
                        await _record_video_publication(
                            cached_source_url,
                            msg,
                            sent_messages,
                            requester_user_id=requester_user_id,
                        )
                else:
                    await handle_parse(
                        cli,
                        msg,
                        url=url,
                        mode=mode,
                        bypass_cache=bypass_cache,
                        force_reupload=force_reupload,
                        requester_user_id=requester_user_id,
                        _t=_t,
                        user_config=user_config,
                    )
                    return
            else:
                logger.debug("Pipeline 返回 None, 跳过后续处理")
            return

        parse_result = result.parse_result
        source_url = await _resolve_result_source_url(url, raw_url, platform_id, parse_result)
        if result.engine != "flyinglife":
            await parse_cache.set(raw_url, parse_result)

        if mode == "preview" and not force_reupload and parse_result.type == PostType.VIDEO:
            publication = await _find_video_publication(source_url, msg)
            if publication and await _prompt_for_duplicate(cli, msg, url, mode, publication):
                await reporter.dismiss()
                return

        # ── 富文本 → Telegraph ──
        if parse_result.type == PostType.RICHTEXT:
            logger.debug(f"富文本类型, 创建 Telegraph 页面: title={parse_result.title}")
            await msg.reply_chat_action(enums.ChatAction.TYPING)
            ph_url = await create_richtext_telegraph(cli, parse_result)
            logger.debug(f"Telegraph 页面创建完成: {ph_url}")
            caption = build_caption(parse_result, ph_url, hide_source=user_config.hide_source)
            await _send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(show_above_text=True),
                )
            )
            await persistent_cache.set(
                raw_url,
                CacheEntry(
                    parse_result=CacheParseResult(
                        title=parse_result.title,
                        content=parse_result.content,
                        raw_url=source_url,
                    ),
                    telegraph_url=ph_url,
                ),
            )
            await reporter.dismiss()
            return

        caption = build_caption(parse_result, hide_source=user_config.hide_source)
        gif_only = all(isinstance(i, AniRef) for i in to_list(parse_result.media))
        if mode == "preview" and gif_only and len(to_list(parse_result.media)) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD:
            await _send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    reply_markup=_build_gif_button(to_list(parse_result.media)),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            await reporter.dismiss()
            return

        if not result.processed_list:
            logger.debug("无媒体文件, 仅发送文本")
            await msg.reply_chat_action(enums.ChatAction.TYPING)
            await _send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            cache_entry = CacheEntry(
                parse_result=CacheParseResult(
                    title=parse_result.title,
                    content=parse_result.content,
                    raw_url=source_url,
                )
            )
            await persistent_cache.set(raw_url, cache_entry)
            await reporter.dismiss()
            return

        if mode == "raw":
            await _send_raw(msg, result, reporter, _t=_t, user_config=user_config)
            return
        if mode == "zip":
            await _send_zip(msg, result, reporter, _t=_t, user_config=user_config)
            return

        # ── 上传媒体 ──
        logger.debug(f"开始上传媒体: media_count={len(result.processed_list)}")
        await reporter.report(_t("上 传 中..."))
        try:
            send_result = await _send_media(msg, parse_result, result.processed_list, caption, _t=_t)
            if send_result.cache_entry:
                await persistent_cache.set(raw_url, send_result.cache_entry)
            if parse_result.type == PostType.VIDEO:
                await _record_video_publication(
                    source_url,
                    msg,
                    send_result.messages,
                    requester_user_id=requester_user_id,
                )
            await reporter.dismiss()
        except Exception as e:
            logger.opt(exception=e).debug("详细堆栈")
            logger.error(f"上传失败: {e}")
            await reporter.report_error(_t("上传"), e)
            return


# ── 构建 InputMedia ──────────────────────────────────────────────────


def _build_input_media(
    media_refs: Sequence[AnyMediaRef],
    processed_list: list[ProcessedMedia],
) -> tuple[list[InputMediaPhoto | InputMediaVideo], list[InputMediaAnimation]]:
    """根据处理结果和媒体引用构建 Telegram InputMedia 列表。

    Returns:
        (photos_videos, animations) 两类媒体列表
    """
    photos_videos: list[InputMediaPhoto | InputMediaVideo] = []
    animations: list[InputMediaAnimation] = []

    for media_ref, processed in zip(media_refs, processed_list, strict=False):
        file_paths = processed.output_paths or [processed.source.path]
        for file_path in file_paths:
            file_path_str = str(file_path)
            width, height, duration = resolve_media_info(processed, file_path_str)

            match processed.source:
                case ImageFile():
                    photos_videos.append(InputMediaPhoto(media=file_path_str))
                case AniFile():
                    animations.append(InputMediaAnimation(media=file_path_str))
                case VideoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=file_path_str,
                            video_cover=media_ref.thumb_url,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )
                case LivePhotoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=processed.source.video_path,
                            video_cover=file_path_str,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )

    return photos_videos, animations


# ── 缓存条目构建 ─────────────────────────────────────────────────────


def _cache_media_from_message(m: Message) -> CacheMedia | None:
    """从已发送的 Telegram Message 提取 CacheMedia。"""
    if m.photo:
        return CacheMedia(type=CacheMediaType.PHOTO, file_id=m.photo.file_id)
    if m.video:
        return CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id=m.video.file_id,
            cover_file_id=m.video.video_cover.file_id if m.video.video_cover else None,
        )
    if m.animation:
        return CacheMedia(type=CacheMediaType.ANIMATION, file_id=m.animation.file_id)
    if m.document:
        return CacheMedia(type=CacheMediaType.DOCUMENT, file_id=m.document.file_id)
    return None


def _make_cache_entry(parse_result: AnyParseResult, media_list: list[CacheMedia]) -> CacheEntry:
    return CacheEntry(
        parse_result=CacheParseResult(
            title=parse_result.title,
            content=parse_result.content,
            raw_url=parse_result.raw_url,
        ),
        media=media_list,
    )


# ── Raw 模式上传 ──────────────────────────────────────────────────────


async def _send_raw(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
    *,
    _t: PreLocaleSelector,
    user_config: UserConfig,
) -> None:
    """Raw 模式：将文件以原始文档形式上传。"""
    logger.debug("Raw 模式, 直接上传文件")
    await reporter.report(_t("上 传 中..."))
    try:
        caption = build_caption(result.parse_result, hide_source=user_config.hide_source)
        docs: list[InputMediaDocument] = []
        gifs = []
        livephoto_videos: dict[int, InputMediaDocument] = {}

        for processed in result.processed_list:
            file_paths = processed.output_paths or [processed.source.path]
            file_path = file_paths[0]
            doc = InputMediaDocument(media=str(file_path))
            if isinstance(processed.source, AniFile):
                gifs.append(doc)
            elif isinstance(processed.source, LivePhotoFile):
                docs.append(doc)
                livephoto_videos[len(docs) - 1] = InputMediaDocument(media=str(processed.source.video_path))
            else:
                docs.append(doc)

        if len(docs + gifs) == 1:
            all_docs = docs + gifs
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            sent_msg = await _send_with_rate_limit(
                lambda: msg.reply_document(_media_input(all_docs[0].media), caption=caption, force_document=True)
            )
            if livephoto_videos and sent_msg:
                await _send_with_rate_limit(
                    lambda: sent_msg.reply_document(_media_input(livephoto_videos[0].media), force_document=True)
                )
        else:
            msgs: list[Message] = []
            for batch in batched(docs, 10):
                await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                # noinspection PyDefaultArgument
                mg = await _send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(b))  # type: ignore
                msgs.extend(mg)
            if livephoto_videos:
                for idx, media_doc in livephoto_videos.items():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                    await _send_with_rate_limit(
                        lambda m_=media_doc, idx_=idx: msgs[idx_].reply_document(  # type: ignore
                            _media_input(m_.media), force_document=True
                        )
                    )
            if gifs:
                await _send_with_rate_limit(
                    lambda: msg.reply_text(
                        _t("**▎GIF 下载链接**"), reply_markup=_build_gif_button(to_list(result.parse_result.media))
                    )
                )
            await _send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )

    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"Raw 模式上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    finally:
        result.cleanup()

    await reporter.dismiss()


async def _send_zip(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
    *,
    _t: PreLocaleSelector,
    user_config: UserConfig,
) -> None:
    logger.debug("Zip 模式, 开始打包")
    await reporter.report(_t("打 包 中..."))
    try:
        caption = build_caption(result.parse_result, hide_source=user_config.hide_source)
        if result.output_dir is None:
            raise ValueError("缺少打包目录")
        pack_path = await asyncio.to_thread(pack_dir_to_tar_gz, result.output_dir)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"打包失败: {e}")
        await reporter.report_error(_t("打包"), Exception("..."))
        return
    finally:
        result.cleanup()

    await reporter.report(_t("上 传 中..."))
    try:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        await _send_with_rate_limit(lambda: msg.reply_document(str(pack_path), caption=caption))
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    finally:
        if not bs.debug_skip_cleanup:
            logger.debug("清理压缩包")
            os.remove(pack_path)

    await reporter.dismiss()


# ── 发送媒体 ─────────────────────────────────────────────────────────


async def _send_single(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
) -> tuple[list[CacheMedia] | None, list[Message]]:
    """发送单个媒体，返回缓存媒体与已发送消息。

    缓存媒体为 None 表示不缓存。
    """
    media_list: list[CacheMedia] = []
    sent_messages: list[Message] = []
    all_media = animations + photos_videos

    try:
        sent: Message | None = None
        if animations:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent = await _send_with_rate_limit(
                lambda: msg.reply_animation(_media_input(animations[0].media), caption=caption)
            )
        else:
            single = photos_videos[0]
            match single:
                case InputMediaPhoto():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
                    sent = await _send_with_rate_limit(
                        lambda: msg.reply_photo(_media_input(single.media), caption=caption)
                    )
                case InputMediaVideo():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
                    try:
                        sent = await _send_with_rate_limit(
                            lambda: msg.reply_video(
                                _media_input(single.media),
                                caption=caption,
                                video_cover=single.video_cover,
                                duration=single.duration,
                                width=single.width,
                                height=single.height,
                                supports_streaming=True,
                            )
                        )
                    except (WebpageCurlFailed, WebpageMediaEmpty):
                        logger.warning("Tg 获取封面失败, 移除封面上传")
                        sent = await _send_with_rate_limit(
                            lambda: msg.reply_video(
                                _media_input(single.media),
                                caption=caption,
                                duration=single.duration,
                                width=single.width,
                                height=single.height,
                                supports_streaming=True,
                            )
                        )

        if sent:
            sent_messages.append(sent)
            if cm := _cache_media_from_message(sent):
                media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        sent = await _send_with_rate_limit(
            lambda: msg.reply_document(_media_input(all_media[0].media), caption=caption, force_document=True)
        )
        return None, [sent] if sent else []

    return media_list, sent_messages


def _build_gif_button(media_refs: Sequence[AnyMediaRef]) -> Ikm:
    buttons = []
    n = 5
    for i, v in enumerate(media_refs):
        if isinstance(v, AniRef):
            buttons.append(Ikb(f"{i + 1}", url=v.url))
    ikbs = [list(i) for i in batched(buttons, n)]
    return Ikm(ikbs)


async def _send_multi(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
    media_refs: Sequence[AnyMediaRef],
    *,
    _t: PreLocaleSelector,
) -> tuple[list[CacheMedia] | None, list[Message]]:
    """发送多个媒体，返回缓存媒体与已发送消息。

    缓存媒体为 None 表示不缓存。
    """
    media_list: list[CacheMedia] = []
    sent_messages: list[Message] = []
    not_cache = False
    if len([i for i in media_refs if isinstance(i, AniRef)]) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD:
        not_cache = True
        await _send_with_rate_limit(
            lambda: msg.reply_text(_t("**▎GIF 过多跳过上传, 请自行下载**"), reply_markup=_build_gif_button(media_refs))
        )
    else:
        for ani in animations:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            caption_ = caption if ani == animations[-1] and not photos_videos else ""
            try:
                sent = await _send_with_rate_limit(
                    lambda a=ani, c=caption_: msg.reply_animation(  # type: ignore[misc]
                        _media_input(a.media),
                        caption=c,
                    )
                )
            except Exception as e:
                logger.warning(f"上传失败 {e}, 使用兼容模式上传")
                not_cache = True
                await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                sent = await _send_with_rate_limit(
                    lambda a=ani, c=caption_: msg.reply_document(  # type: ignore[misc]
                        _media_input(a.media), caption=c, force_document=True
                    )
                )
            else:
                # 过大的 GIF 会返回 document
                if sent and sent.document:
                    media_list.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=sent.document.file_id))
                elif sent and sent.animation:
                    media_list.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=sent.animation.file_id))
            if sent:
                sent_messages.append(sent)

    try:
        for batch in batched(photos_videos, 10):
            if batch[-1] == photos_videos[-1]:
                batch[0].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            # noinspection PyDefaultArgument
            sent_msgs = await _send_with_rate_limit(
                lambda b=list(batch): msg.reply_media_group(media=b)  # type: ignore[misc]
            )
            sent_messages.extend(sent_msgs)
            for m in sent_msgs:
                if cm := _cache_media_from_message(m):
                    media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        input_documents: list[InputMediaDocument] = [
            InputMediaDocument(media=_media_input(item.media)) for item in photos_videos
        ]
        for document_batch in batched(input_documents, 10):
            if document_batch[-1] == input_documents[-1]:
                document_batch[-1].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            # noinspection PyDefaultArgument
            sent_msgs = await _send_with_rate_limit(
                lambda b=list(document_batch): msg.reply_media_group(media=b)  # type: ignore
            )
            sent_messages.extend(sent_msgs)
        return None, sent_messages

    return (None if not_cache else media_list), sent_messages


async def _send_media(
    msg: Message,
    parse_result: AnyParseResult,
    processed_list: list[ProcessedMedia],
    caption: str,
    *,
    _t: PreLocaleSelector,
) -> MediaSendResult:
    """构建、发送媒体，并返回缓存条目与已发送消息。
    """
    media_refs = to_list(parse_result.media)
    photos_videos, animations = _build_input_media(media_refs, processed_list)
    all_count = len(photos_videos) + len(animations)
    logger.debug(f"媒体分类完成: animations={len(animations)}, photos_videos={len(photos_videos)}")

    if all_count == 1:
        logger.debug("单媒体模式发送")
        media_list, sent_messages = await _send_single(msg, photos_videos, animations, caption)
    else:
        logger.debug(f"多媒体模式发送: total={all_count}")
        media_list, sent_messages = await _send_multi(msg, photos_videos, animations, caption, media_refs, _t=_t)

    if media_list is None:
        return MediaSendResult(cache_entry=None, messages=sent_messages)
    return MediaSendResult(cache_entry=_make_cache_entry(parse_result, media_list), messages=sent_messages)


# ── 缓存发送 ─────────────────────────────────────────────────────────


async def _send_cached(msg: Message, entry: CacheEntry, url: str, *, user_config: UserConfig) -> list[Message]:
    """从 file_id 缓存直接发送，跳过解析/下载/转码"""
    logger.debug(f"缓存发送: media={entry.media}")
    caption = build_caption_by_str(
        entry.parse_result.title,
        entry.parse_result.content,
        _cache_entry_source_url(entry, url),
        entry.telegraph_url,
        hide_source=user_config.hide_source,
    )

    # 富文本类型
    if entry.telegraph_url:
        sent = await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(show_above_text=True),
        )
        return [sent]

    if not entry.media:
        sent = await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return [sent]

    if len(entry.media) == 1:
        return await _send_cached_single(msg, entry.media[0], caption)
    return await _send_cached_multi(msg, entry.media, caption)


async def _send_cached_single(msg: Message, m: CacheMedia, caption: str) -> list[Message]:
    """从缓存发送单个媒体。"""
    match m.type:
        case CacheMediaType.PHOTO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent = await _send_with_rate_limit(lambda: msg.reply_photo(m.file_id, caption=caption))
        case CacheMediaType.VIDEO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
            sent = await _send_with_rate_limit(
                lambda: msg.reply_video(
                    m.file_id, caption=caption, supports_streaming=True, video_cover=m.cover_file_id
                )
            )
        case CacheMediaType.ANIMATION:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent = await _send_with_rate_limit(lambda: msg.reply_animation(m.file_id, caption=caption))
        case CacheMediaType.DOCUMENT:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            sent = await _send_with_rate_limit(
                lambda: msg.reply_document(m.file_id, caption=caption, force_document=True)
            )
    return [sent] if sent else []


async def _send_cached_multi(msg: Message, media: list[CacheMedia], caption: str) -> list[Message]:
    """从缓存发送多个媒体。"""
    animations = [m for m in media if m.type == CacheMediaType.ANIMATION]
    others = [m for m in media if m.type != CacheMediaType.ANIMATION]
    sent_messages: list[Message] = []

    for ani in animations:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        sent = await _send_with_rate_limit(
            lambda a=ani: msg.reply_animation(  # type: ignore[misc]
                a.file_id,
                caption=caption if a == animations[-1] and not others else "",
            )
        )
        if sent:
            sent_messages.append(sent)

    media_group = _build_cached_media_group(others)
    for batch in batched(media_group, 10):
        if batch[-1] == media_group[-1]:
            batch[0].caption = caption

        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        # noinspection PyDefaultArgument
        sent_messages.extend(
            await _send_with_rate_limit(lambda m=list(batch): msg.reply_media_group(m))  # type: ignore[misc]
        )
    return sent_messages


def _build_cached_media_group(
    media: list[CacheMedia],
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """从 CacheMedia 列表构建 Telegram media group。"""
    group: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for m in media:
        match m.type:
            case CacheMediaType.PHOTO:
                group.append(InputMediaPhoto(media=m.file_id))
            case CacheMediaType.VIDEO:
                if m.cover_file_id:
                    group.append(InputMediaVideo(media=m.file_id, supports_streaming=True, video_cover=m.cover_file_id))
                else:
                    group.append(InputMediaVideo(media=m.file_id, supports_streaming=True))
            case CacheMediaType.DOCUMENT:
                group.append(InputMediaDocument(media=m.file_id))
    return group
