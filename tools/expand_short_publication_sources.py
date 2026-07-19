"""Expand short Source links, edit Telegram captions, and update publication history.

The tool scans explicit message IDs because Telegram does not expose group
history to bots. It reuses stable IDs already stored locally, resolves only the
remaining short links, journals the original caption entities, edits the Source
link without changing caption text, and records successfully edited messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pyrogram import Client, enums, raw
from pyrogram.errors import FloodWait
from pyrogram.types import Message, MessageEntity
from sqlalchemy import func, select

from core import bs
from db import get_session
from db.engine import close_db
from db.models.media_publication import MediaPublication
from repo.media_publication import MediaPublicationRepo
from services.parser import ParseService
from services.publication_history import (
    douyin_content_id,
    make_content_key,
    normalize_source_url,
    safe_message_link,
    topic_title,
)

TELEGRAM_BATCH_SIZE = 200


def _chunks(values: range, size: int = TELEGRAM_BATCH_SIZE) -> Iterable[list[int]]:
    for start in range(values.start, values.stop, size):
        yield list(range(start, min(start + size, values.stop)))


def _has_publication_media(message: Message) -> bool:
    return any(
        (
            message.video,
            message.photo,
            message.animation,
            message.audio,
            message.document,
            message.voice,
            message.video_note,
        )
    )


@dataclass(frozen=True, slots=True)
class SourcePlan:
    message: Message
    entity_index: int
    short_url: str
    canonical_url: str
    resolution: str
    caption: str
    caption_entities: tuple[MessageEntity, ...]


@dataclass(slots=True)
class ExpandStats:
    scanned_ids: int = 0
    live_messages: int = 0
    short_sources: int = 0
    local_sources_reused: int = 0
    remotely_resolved: int = 0
    resolve_failed: int = 0
    edit_succeeded: int = 0
    edit_failed: int = 0
    db_inserted: int = 0
    db_updated: int = 0
    db_kept_newer: int = 0


def normalize_supergroup_id(value: int) -> int:
    if value <= -1_000_000_000_000:
        return value
    return int(f"-100{abs(value)}")


def _entity_text(text: str, entity: MessageEntity) -> str:
    encoded = text.encode("utf-16-le")
    start = entity.offset * 2
    end = start + entity.length * 2
    return encoded[start:end].decode("utf-16-le")


def source_entity(message: Message) -> tuple[int, str] | None:
    if not _has_publication_media(message) or not message.caption:
        return None
    entities = message.caption_entities or []
    for index in range(len(entities) - 1, -1, -1):
        entity = entities[index]
        if entity.type != enums.MessageEntityType.TEXT_LINK or not entity.url:
            continue
        if _entity_text(message.caption, entity).casefold() != "source":
            continue
        normalized = normalize_source_url(entity.url)
        return index, normalized
    return None


def short_source_entity(message: Message) -> tuple[int, str] | None:
    source = source_entity(message)
    if source is None:
        return None
    host = (urlsplit(source[1]).hostname or "").lower()
    if (host == "douyin.com" or host.endswith(".douyin.com")) and not douyin_content_id(source[1]):
        return source
    return None


def _clone_entity(entity: MessageEntity, *, url: str | None = None) -> MessageEntity:
    return MessageEntity(
        type=entity.type,
        offset=entity.offset,
        length=entity.length,
        url=url if url is not None else entity.url,
        user=entity.user,
        language=entity.language,
        custom_emoji_id=entity.custom_emoji_id,
        expandable=entity.expandable,
        unix_time=entity.unix_time,
        date_time_format=entity.date_time_format,
    )


def updated_caption_entities(plan: SourcePlan) -> list[MessageEntity]:
    return [
        _clone_entity(entity, url=plan.canonical_url if index == plan.entity_index else None)
        for index, entity in enumerate(plan.caption_entities)
    ]


async def _local_sources(chat_id: int) -> dict[int, str]:
    async with get_session() as session:
        rows = list(
            await session.execute(
                select(MediaPublication.message_id, MediaPublication.source_url).where(
                    MediaPublication.chat_id == chat_id
                )
            )
        )
    return {
        message_id: normalize_source_url(source_url)
        for message_id, source_url in rows
        if douyin_content_id(source_url)
    }


async def _resolve_one(short_url: str, semaphore: asyncio.Semaphore) -> str | None:
    async with semaphore:
        try:
            resolved = normalize_source_url(await ParseService().get_raw_url(short_url))
        except Exception as error:
            print(f"resolve failed url={short_url} error={type(error).__name__}")
            return None
    return resolved if douyin_content_id(resolved) else None


async def build_plan(
    client: Client,
    chat_id: int,
    max_message_id: int,
    *,
    concurrency: int,
    stats: ExpandStats,
) -> tuple[list[SourcePlan], list[dict[str, object]]]:
    local_sources = await _local_sources(chat_id)
    candidates: list[tuple[Message, int, str]] = []
    stats.scanned_ids = max_message_id

    for message_ids in _chunks(range(1, max_message_id + 1), TELEGRAM_BATCH_SIZE):
        messages = await client.get_messages(chat_id, message_ids, replies=0)
        if isinstance(messages, Message):
            messages = [messages]
        for message in messages:
            if message is None or getattr(message, "empty", False):
                continue
            stats.live_messages += 1
            source = short_source_entity(message)
            if source is not None:
                candidates.append((message, source[0], source[1]))
        print(f"scan through={message_ids[-1]} live={stats.live_messages} short_sources={len(candidates)}")

    stats.short_sources = len(candidates)
    unresolved_urls = list(
        dict.fromkeys(short_url for message, _, short_url in candidates if message.id not in local_sources)
    )
    semaphore = asyncio.Semaphore(concurrency)
    resolved_values = await asyncio.gather(*(_resolve_one(url, semaphore) for url in unresolved_urls))
    resolved_by_url = dict(zip(unresolved_urls, resolved_values, strict=True))

    plans: list[SourcePlan] = []
    journal: list[dict[str, object]] = []
    for message, entity_index, short_url in candidates:
        canonical_url = local_sources.get(message.id)
        resolution = "local"
        if canonical_url is None:
            canonical_url = resolved_by_url.get(short_url)
            resolution = "expanded"
        if canonical_url is None:
            stats.resolve_failed += 1
            continue
        if resolution == "local":
            stats.local_sources_reused += 1
        else:
            stats.remotely_resolved += 1
        plans.append(
            SourcePlan(
                message=message,
                entity_index=entity_index,
                short_url=short_url,
                canonical_url=canonical_url,
                resolution=resolution,
                caption=message.caption or "",
                caption_entities=tuple(message.caption_entities or []),
            )
        )
        journal.append(
            {
                "chat_id": chat_id,
                "message_id": message.id,
                "message_link": safe_message_link(message),
                "caption": message.caption,
                "caption_entities": [
                    {
                        "type": entity.type.name,
                        "offset": entity.offset,
                        "length": entity.length,
                        "url": entity.url,
                    }
                    for entity in message.caption_entities or []
                ],
                "old_source_url": short_url,
                "new_source_url": canonical_url,
                "resolution": resolution,
            }
        )
    return plans, journal


def _journal_entity(item: dict[str, object]) -> MessageEntity:
    return MessageEntity(
        type=enums.MessageEntityType[str(item["type"])],
        offset=int(item["offset"]),
        length=int(item["length"]),
        url=str(item["url"]) if item.get("url") else None,
    )


async def load_journal_plans(client: Client, path: Path) -> tuple[int, list[SourcePlan]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    chat_id = int(payload["chat_id"])
    entries = list(payload["messages"])
    message_ids = [int(entry["message_id"]) for entry in entries]
    messages = await client.get_messages(chat_id, message_ids, replies=0)
    if isinstance(messages, Message):
        messages = [messages]
    by_id = {message.id: message for message in messages if not getattr(message, "empty", False)}

    plans: list[SourcePlan] = []
    for entry in entries:
        message_id = int(entry["message_id"])
        message = by_id.get(message_id)
        if message is None:
            print(f"journal message missing message_id={message_id}")
            continue
        caption = str(entry["caption"])
        entities = tuple(_journal_entity(item) for item in entry["caption_entities"])
        entity_index = next(
            (
                index
                for index, entity in enumerate(entities)
                if entity.type == enums.MessageEntityType.TEXT_LINK
                and _entity_text(caption, entity).casefold() == "source"
            ),
            None,
        )
        if entity_index is None:
            print(f"journal Source entity missing message_id={message_id}")
            continue
        plans.append(
            SourcePlan(
                message=message,
                entity_index=entity_index,
                short_url=str(entry["old_source_url"]),
                canonical_url=str(entry["new_source_url"]),
                resolution=str(entry["resolution"]),
                caption=caption,
                caption_entities=entities,
            )
        )
    return chat_id, plans


async def _topic_titles(client: Client, chat_id: int, plans: list[SourcePlan]) -> dict[int, str]:
    titles: dict[int, str] = {}
    thread_ids = sorted({plan.message.message_thread_id or 0 for plan in plans})
    for thread_id in thread_ids:
        if not thread_id:
            continue
        try:
            topic = await client.get_forum_topics_by_id(chat_id, thread_id)
        except Exception as error:
            print(f"topic lookup failed thread_id={thread_id} error={type(error).__name__}")
            continue
        title = getattr(topic, "title", None)
        if title:
            titles[thread_id] = str(title)
    return titles


async def edit_sources(client: Client, plans: list[SourcePlan], stats: ExpandStats) -> list[SourcePlan]:
    requested: list[SourcePlan] = []
    for plan in plans:
        message = plan.message
        current_source = source_entity(message)
        if current_source is not None and current_source[1] == plan.canonical_url:
            requested.append(plan)
            print(f"already canonical message_id={message.id}")
            continue
        entities = updated_caption_entities(plan)
        accepted = False
        for attempt in range(1, 4):
            try:
                await client.invoke(
                    raw.functions.messages.EditMessage(
                        peer=await client.resolve_peer(message.chat.id),
                        id=message.id,
                        message=plan.caption,
                        entities=[await entity.write() for entity in entities],
                        invert_media=message.show_caption_above_media,
                        reply_markup=(
                            await message.reply_markup.write(client) if message.reply_markup is not None else None
                        ),
                    )
                )
            except FloodWait as error:
                wait_seconds = int(error.value) + 1
                print(f"edit flood wait seconds={wait_seconds} message_id={message.id}")
                await asyncio.sleep(wait_seconds)
                continue
            except Exception as error:
                if attempt == 3:
                    print(f"edit failed message_id={message.id} error={type(error).__name__}")
                else:
                    await asyncio.sleep(2)
                continue
            requested.append(plan)
            accepted = True
            print(f"edited message_id={message.id} source={plan.canonical_url}")
            await asyncio.sleep(0.75)
            break
        if not accepted:
            stats.edit_failed += 1

    verified: list[SourcePlan] = []
    if not requested:
        return verified
    plans_by_id = {plan.message.id: plan for plan in requested}
    requested_ids = list(plans_by_id)
    for start in range(0, len(requested_ids), TELEGRAM_BATCH_SIZE):
        messages = await client.get_messages(
            requested[0].message.chat.id,
            requested_ids[start : start + TELEGRAM_BATCH_SIZE],
            replies=0,
        )
        if isinstance(messages, Message):
            messages = [messages]
        for edited in messages:
            plan = plans_by_id.get(edited.id)
            if plan is None or getattr(edited, "empty", False):
                continue
            edited_source = source_entity(edited)
            if edited_source is None or edited_source[1] != plan.canonical_url:
                stats.edit_failed += 1
                print(f"edit verification failed message_id={edited.id}")
                continue
            stats.edit_succeeded += 1
            verified.append(plan)
    return verified


async def update_database(
    chat_id: int,
    plans: list[SourcePlan],
    titles: dict[int, str],
    stats: ExpandStats,
) -> tuple[int, int]:
    async with get_session() as session:
        before = int(
            await session.scalar(select(func.count()).select_from(MediaPublication).where(MediaPublication.chat_id == chat_id))
            or 0
        )
        repo = MediaPublicationRepo(session)
        for plan in sorted(plans, key=lambda item: item.message.id):
            message = plan.message
            key = make_content_key(plan.canonical_url)
            existing = await repo.get(content_key=key, chat_id=chat_id)
            if existing is not None and existing.message_id > message.id:
                stats.db_kept_newer += 1
                continue
            was_existing = existing is not None
            published_at = message.date or datetime.now(UTC)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=UTC)
            thread_id = message.message_thread_id or 0
            await repo.upsert(
                content_key=key,
                source_url=plan.canonical_url,
                chat_id=chat_id,
                message_thread_id=thread_id,
                topic_title=titles.get(thread_id) or topic_title(message),
                message_id=message.id,
                message_link=safe_message_link(message),
                requester_user_id=None,
                published_at=published_at,
            )
            if was_existing:
                stats.db_updated += 1
            else:
                stats.db_inserted += 1
        await session.flush()
        after = int(
            await session.scalar(select(func.count()).select_from(MediaPublication).where(MediaPublication.chat_id == chat_id))
            or 0
        )
    return before, after


def write_journal(path: Path, chat_id: int, journal: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "chat_id": chat_id,
        "messages": journal,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args: argparse.Namespace) -> None:
    stats = ExpandStats()
    async with Client(
        ":memory:",
        api_id=int(bs.api_id),
        api_hash=bs.api_hash,
        bot_token=bs.bot_token,
        proxy=bs.bot_proxy,
    ) as client:
        if args.resume_journal is not None:
            chat_id, plans = await load_journal_plans(client, args.resume_journal)
            stats.short_sources = len(plans)
            stats.local_sources_reused = sum(plan.resolution == "local" for plan in plans)
            stats.remotely_resolved = sum(plan.resolution == "expanded" for plan in plans)
            print(f"resume journal={args.resume_journal} planned={len(plans)}")
            journal: list[dict[str, object]] = []
        else:
            assert args.chat_id is not None and args.max_message_id is not None
            chat_id = normalize_supergroup_id(args.chat_id)
            plans, journal = await build_plan(
                client,
                chat_id,
                args.max_message_id,
                concurrency=args.concurrency,
                stats=stats,
            )
        chat = await client.get_chat(chat_id)
        print(f"group id={chat_id} title={chat.title!r}")
        print(
            f"plan short_sources={stats.short_sources} planned={len(plans)} "
            f"local={stats.local_sources_reused} expanded={stats.remotely_resolved} "
            f"resolve_failed={stats.resolve_failed}"
        )
        if not args.apply:
            print("dry run only; pass --apply to edit Telegram and update the database")
            return
        if args.resume_journal is None:
            write_journal(args.journal, chat_id, journal)
            print(f"journal={args.journal}")
        titles = await _topic_titles(client, chat_id, plans)
        succeeded = await edit_sources(client, plans, stats)

    before, after = await update_database(chat_id, succeeded, titles, stats)
    print(
        f"complete edited={stats.edit_succeeded} edit_failed={stats.edit_failed} "
        f"db_before={before} db_after={after} db_inserted={stats.db_inserted} "
        f"db_updated={stats.db_updated} db_kept_newer={stats.db_kept_newer}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id", type=int, nargs="?")
    parser.add_argument("--max-message-id", type=int)
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--journal", type=Path, default=Path("data/backups/short-source-edit-journal.json"))
    parser.add_argument("--resume-journal", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.resume_journal is None and (args.chat_id is None or not args.max_message_id or args.max_message_id < 1):
        raise ValueError("chat_id and a positive --max-message-id are required unless --resume-journal is used")
    try:
        await run(args)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
