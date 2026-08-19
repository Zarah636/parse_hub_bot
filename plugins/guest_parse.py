from typing import cast

from pyrogram import Client
from pyrogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    SentGuestMessage,
)

from db import get_session
from i18n import t_
from log import logger
from plugins.helpers import build_caption, build_caption_by_str, format_label
from plugins.parse.reporters import InlineStatusReporter
from repo.settings import SettingsConfig
from services import (
    CacheEntry,
    CacheParseResult,
    HybridParsePipeline,
    ParseService,
    SettingsService,
    UserService,
    flyinglife,
)
from services.cache import parse_cache, persistent_cache
from services.guest_rich_message import (
    RichMessageUnsupported,
    build_cached_rich_message,
    build_pipeline_rich_message,
    edit_inline_rich_message,
)
from utils.helpers import with_request_id

logger = logger.bind(name="GuestParse")


def extract_guest_url(message: Message) -> str | None:
    reference = message.reply_to_message
    text = (reference.text or reference.caption or "") if reference else ""
    for item in text.split():
        if ParseService().parser.get_platform(item):
            return item
    return None


async def answer_guest_text(cli: Client, query_id: str, title: str, text: str) -> SentGuestMessage:
    return cast(
        SentGuestMessage,
        await cli.answer_guest_query(
            query_id,
            InlineQueryResultArticle(
                title=title,
                input_message_content=InputTextMessageContent(
                    text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                ),
            ),
        ),
    )


async def send_cached_guest(
    cli: Client,
    inline_message_id: str,
    cached: CacheEntry,
    raw_url: str,
    config: SettingsConfig,
) -> bool:
    """使用原项目的 Telegram file_id 缓存更新 Guest 消息。"""
    try:
        rich = build_cached_rich_message(
            cached,
            raw_url,
            hide_title=config.hide_title,
            hide_desc=config.hide_desc,
            hide_source=config.hide_source,
        )
        await edit_inline_rich_message(cli, inline_message_id, rich.message)
        logger.info(f"Guest 使用 Telegram 持久缓存: layout={rich.layout}, media={rich.media_count}")
        return True
    except RichMessageUnsupported as e:
        logger.warning(f"Guest 缓存不适用 Rich Message: {e}")
        caption = build_caption_by_str(
            cached.parse_result.title,
            cached.parse_result.content,
            raw_url,
            cached.telegraph_url,
            hide_source=config.hide_source,
            hide_title=config.hide_title,
            hide_desc=config.hide_desc,
            rich=cached.rich,
        )
        await cli.edit_inline_text(inline_message_id, text=caption)
        return True
    except Exception as e:
        logger.warning(f"Guest 缓存发送失败, 删除失效缓存并重新解析: {type(e).__name__}: {e}")
        try:
            await persistent_cache.remove(raw_url)
        except Exception as remove_error:
            logger.warning(f"Guest 删除失效缓存失败: {type(remove_error).__name__}: {remove_error}")
        return False


@Client.on_guest_message()
@with_request_id
async def guest_parse(cli: Client, msg: Message) -> None:
    if not msg.guest_query_id or not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session).get_lang(msg.from_user.id)
        config = await SettingsService(session).get_config_by_user(msg.from_user.id)
    _t = t_[lang]

    url = extract_guest_url(msg)
    if not url:
        await answer_guest_text(
            cli,
            msg.guest_query_id,
            _t("聚合解析"),
            format_label(_t("请回复一条包含支持链接的消息")),
        )
        return

    sent = await answer_guest_text(
        cli,
        msg.guest_query_id,
        _t("聚合解析"),
        format_label(_t("解 析 中...")),
    )
    inline_message_id = sent.inline_message_id
    reporter = InlineStatusReporter(
        cli,
        inline_message_id,
        t=_t,
        user_config=config,
        failure_text=format_label(_t("解析失败，请重新尝试。")),
    )
    parse_service = ParseService()

    try:
        platform_id = parse_service.get_platform(url).id
        use_flyinglife = flyinglife.should_attempt(platform_id, context="guest")
        raw_url = await parse_service.get_raw_url(url)
    except Exception as e:
        await reporter.report_error(_t("获取原始链接"), e)
        return

    try:
        cached = await persistent_cache.get(raw_url)
    except Exception as e:
        logger.warning(f"Guest 读取持久缓存失败, 继续解析: {type(e).__name__}: {e}")
        cached = None
    if cached:
        if await send_cached_guest(cli, inline_message_id, cached, raw_url, config):
            return

    cached_parse_result = await parse_cache.get(raw_url)
    with HybridParsePipeline(
        url,
        raw_url,
        reporter,
        parse_result=cached_parse_result,
        platform_id=platform_id,
        singleflight=True,
        download_video_cover=config.video_cover,
        skip_flyinglife=not use_flyinglife,
        t=_t,
    ) as pipeline:
        result = await pipeline.run()
        if result is None:
            return
        if result.engine != "flyinglife":
            await parse_cache.set(raw_url, result.parse_result)

        await reporter.report(_t("上 传 中..."))
        try:
            rich = await build_pipeline_rich_message(
                cli,
                result,
                hide_title=config.hide_title,
                hide_desc=config.hide_desc,
                hide_source=config.hide_source,
            )
            await edit_inline_rich_message(cli, inline_message_id, rich.message)
            if rich.cache_media is not None:
                try:
                    await persistent_cache.set(
                        raw_url,
                        CacheEntry(
                            parse_result=CacheParseResult(
                                title=result.parse_result.title,
                                content=result.parse_result.content,
                            ),
                            media=rich.cache_media,
                        ),
                    )
                except Exception as cache_error:
                    logger.warning(f"Guest 写入持久缓存失败: {type(cache_error).__name__}: {cache_error}")
            logger.info(f"Guest 解析完成: engine={result.engine}, layout={rich.layout}, media={rich.media_count}")
            return
        except RichMessageUnsupported as e:
            logger.warning(f"Guest Rich Message 降级: {e}")
            caption = build_caption(result.parse_result, config=config)
            notice = _t("媒体数量过多或类型不受 Guest Rich Message 支持，请私聊 Bot 解析。")
            await cli.edit_inline_text(inline_message_id, text=f"{caption}\n\n{format_label(notice)}")
            return
        except Exception as e:
            logger.opt(exception=e).debug("Guest Rich Message 详细堆栈")
            await reporter.report_error(_t("上传"), e)
            return
