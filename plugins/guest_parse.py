from easy_ai18n import PreLocaleSelector
from pyrogram import Client, raw
from pyrogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
)

from db import get_session
from i18n import t_
from log import logger
from plugins.helpers import build_caption, build_caption_by_str, format_label
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
    RawRichMessageContent,
    RichMessageUnsupported,
    build_cached_rich_message,
    build_pipeline_rich_message,
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


class GuestStatusReporter:
    """Collect pipeline status because a guest query can only be answered once."""

    def __init__(
        self,
        *,
        t: PreLocaleSelector,
        user_config: SettingsConfig,
        failure_text: str,
    ) -> None:
        self._t = t
        self._user_config = user_config
        self._failure_text = failure_text
        self._last_text: str | None = None
        self._error: tuple[str, Exception] | None = None

    async def report(self, text: str) -> None:
        self._last_text = text

    async def report_error(self, stage: str, error: Exception) -> None:
        self._error = (stage, error)

    async def dismiss(self) -> None:
        pass

    def final_failure_text(self) -> str:
        if self._error is not None and not self._user_config.hide_error:
            stage, error = self._error
            label = format_label(self._t(f"{stage}错误:"))
            return self._t(f"{label} \n```\n{error}```")
        if self._error is None and self._last_text:
            return format_label(self._last_text)
        return self._failure_text


async def answer_guest_text(cli: Client, query_id: str, title: str, text: str) -> None:
    await cli.answer_guest_query(
        query_id,
        InlineQueryResultArticle(
            title=title,
            input_message_content=InputTextMessageContent(
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            ),
        ),
    )


async def answer_guest_rich(
    cli: Client,
    query_id: str,
    title: str,
    message: raw.types.InputRichMessage,
) -> None:
    await cli.answer_guest_query(
        query_id,
        InlineQueryResultArticle(
            title=title,
            input_message_content=RawRichMessageContent(message),
        ),
    )


async def send_cached_guest(
    cli: Client,
    query_id: str,
    cached: CacheEntry,
    raw_url: str,
    config: SettingsConfig,
    *,
    title: str,
) -> bool:
    """使用原项目的 Telegram file_id 缓存直接回答 Guest 请求。"""
    try:
        rich = build_cached_rich_message(
            cached,
            raw_url,
            hide_title=config.hide_title,
            hide_desc=config.hide_desc,
            hide_source=config.hide_source,
        )
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
        await answer_guest_text(cli, query_id, title, caption)
        return True
    except Exception as e:
        logger.warning(f"Guest 缓存发送失败, 删除失效缓存并重新解析: {type(e).__name__}: {e}")
        try:
            await persistent_cache.remove(raw_url)
        except Exception as remove_error:
            logger.warning(f"Guest 删除失效缓存失败: {type(remove_error).__name__}: {remove_error}")
        return False

    await answer_guest_rich(cli, query_id, title, rich.message)
    logger.info(f"Guest 使用 Telegram 持久缓存: layout={rich.layout}, media={rich.media_count}")
    return True


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

    title = _t("聚合解析")
    reporter = GuestStatusReporter(
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
        await answer_guest_text(cli, msg.guest_query_id, title, reporter.final_failure_text())
        return

    try:
        cached = await persistent_cache.get(raw_url)
    except Exception as e:
        logger.warning(f"Guest 读取持久缓存失败, 继续解析: {type(e).__name__}: {e}")
        cached = None
    if cached:
        if await send_cached_guest(cli, msg.guest_query_id, cached, raw_url, config, title=title):
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
            await answer_guest_text(cli, msg.guest_query_id, title, reporter.final_failure_text())
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
            await answer_guest_rich(cli, msg.guest_query_id, title, rich.message)
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
            await answer_guest_text(
                cli,
                msg.guest_query_id,
                title,
                f"{caption}\n\n{format_label(notice)}",
            )
            return
        except Exception as e:
            logger.opt(exception=e).debug("Guest Rich Message 详细堆栈")
            await reporter.report_error(_t("上传"), e)
            try:
                await answer_guest_text(cli, msg.guest_query_id, title, reporter.final_failure_text())
            except Exception as answer_error:
                logger.warning(f"Guest 最终失败消息发送失败: {type(answer_error).__name__}: {answer_error}")
            return
