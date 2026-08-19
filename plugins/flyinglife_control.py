from easy_ai18n import PreLocaleSelector
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import CallbackQuery, Message
from pyrogram.types import InlineKeyboardButton as Ikb
from pyrogram.types import InlineKeyboardMarkup as Ikm

from core import bs
from db import get_session
from i18n import t_
from plugins.helpers import format_label
from services import UserService, flyinglife_runtime, inline_flyinglife_cache


def _render_panel(_t: PreLocaleSelector) -> tuple[str, Ikm]:
    if not bs.flyinglife_enabled:
        text = format_label(_t("FlyingLife 优先解析"))
        text += f"\n\n{_t('部署配置未启用 FlyingLife，运行时开关暂不可用。')}"
        return text, Ikm([[Ikb(_t("当前不可用"), callback_data="flctl|noop")]])

    enabled = flyinglife_runtime.preferred
    state = _t("已开启") if enabled else _t("已关闭")
    text = format_label(_t("FlyingLife 优先解析"))
    text += f"\n\n{_t('全局状态')}: {'🟢' if enabled else '⚫'} {state}"
    text += f"\n\n{_t('同时控制普通解析、内联解析和 Guest 解析。')}"
    button = Ikb(
        _t("关闭 FlyingLife 优先解析") if enabled else _t("开启 FlyingLife 优先解析"),
        callback_data=f"flctl|set|{0 if enabled else 1}",
        style=ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS,
    )
    return text, Ikm([[button]])


async def _get_t(user_id: int) -> PreLocaleSelector:
    async with get_session() as session:
        lang = await UserService(session).get_lang(user_id)
    return t_[lang]


@Client.on_message(filters.command("flyinglife"))
async def flyinglife_panel(_: Client, msg: Message) -> None:
    if not msg.from_user:
        return
    text, markup = _render_panel(await _get_t(msg.from_user.id))
    await msg.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^flctl\|"))
async def flyinglife_callback(_: Client, cq: CallbackQuery) -> None:
    if not cq.data or not cq.message:
        return
    _t = await _get_t(cq.from_user.id)
    parts = str(cq.data).split("|")
    if parts[1] == "noop" or not bs.flyinglife_enabled:
        await cq.answer(_t("请先在部署配置中启用 FlyingLife"), show_alert=True)
        return
    if len(parts) != 3 or parts[1] != "set" or parts[2] not in {"0", "1"}:
        await cq.answer(_t("无效操作"), show_alert=True)
        return

    enabled = await flyinglife_runtime.set_preferred(parts[2] == "1")
    if not enabled:
        await inline_flyinglife_cache.clear()
    text, markup = _render_panel(_t)
    await cq.message.edit_text(text, reply_markup=markup)
    await cq.answer(_t("已更新"))
