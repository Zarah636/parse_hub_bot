"""Rebuild one Telegram group's publication history from live messages.

Bots cannot call Telegram's ``messages.GetHistory`` method.  This tool therefore
reads explicit message IDs in batches, ignores deleted messages, extracts stable
work IDs from Source links, and atomically replaces only the selected group's
database rows. No remote parsing or short-link expansion is performed.

Example::

    python -m tools.rebuild_group_publications 4345529300 --max-message-id 2000 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pyrogram import Client, enums
from pyrogram.types import Message
from sqlalchemy import delete, select

from core import bs
from db import get_session
from db.engine import close_db
from db.models.media_publication import MediaPublication
from services.publication_history import (
    douyin_content_id,
    make_content_key,
    normalize_source_url,
    safe_message_link,
    topic_title,
)

TELEGRAM_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class RebuildRecord:
    content_key: str
    source_url: str
    chat_id: int
    message_thread_id: int
    topic_title: str | None
    message_id: int
    message_link: str | None
    published_at: datetime


@dataclass(slots=True)
class RebuildStats:
    scanned_ids: int = 0
    live_messages: int = 0
    source_messages: int = 0
    stable_sources: int = 0
    local_sources_reused: int = 0
    skipped_short_sources: int = 0
    duplicate_sources: int = 0
    topic_failures: int = 0


def normalize_supergroup_id(value: int) -> int:
    """Accept either ``4345529300`` or Telegram Bot API's ``-1004345529300``."""
    if value <= -1_000_000_000_000:
        return value
    if value < 0:
        value = abs(value)
    return int(f"-100{value}")


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


def source_url_from_publication(message: Message) -> str | None:
    """Return the final linked URL from a media caption.

    ParseHub appends its Source link at the end of a publication caption.  A
    canonical Douyin work URL is preferred when older captions contain more
    than one linked entity.
    """
    if not _has_publication_media(message):
        return None

    entities = message.caption_entities or message.entities or []
    links = [
        entity.url
        for entity in entities
        if entity.type == enums.MessageEntityType.TEXT_LINK and entity.url
    ]
    for url in reversed(links):
        if douyin_content_id(url):
            return normalize_source_url(url)
    return normalize_source_url(links[-1]) if links else None


async def _topic_titles(client: Client, chat_id: int, thread_ids: set[int], stats: RebuildStats) -> dict[int, str]:
    titles: dict[int, str] = {}
    for thread_id in sorted(thread_ids):
        if not thread_id:
            continue
        try:
            topic = await client.get_forum_topics_by_id(chat_id, thread_id)
        except Exception as error:
            stats.topic_failures += 1
            print(f"topic lookup failed thread_id={thread_id} error={type(error).__name__}")
            continue
        title = getattr(topic, "title", None)
        if title:
            titles[thread_id] = str(title)
    return titles


async def _local_sources_by_message_id(chat_id: int) -> dict[int, str]:
    """Reuse already-known stable IDs without making any network request."""
    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(MediaPublication.message_id, MediaPublication.source_url).where(
                        MediaPublication.chat_id == chat_id
                    )
                )
            )
        )
    return {
        message_id: normalize_source_url(source_url)
        for message_id, source_url in rows
        if douyin_content_id(source_url)
    }


async def scan_group(
    client: Client,
    chat_id: int,
    max_message_id: int,
    local_sources_by_message_id: dict[int, str],
) -> tuple[list[RebuildRecord], RebuildStats]:
    stats = RebuildStats(scanned_ids=max_message_id)
    candidates: list[tuple[Message, str]] = []

    for message_ids in _chunks(range(1, max_message_id + 1)):
        messages = await client.get_messages(chat_id, message_ids, replies=0)
        if isinstance(messages, Message):
            messages = [messages]
        for message in messages:
            if message is None or getattr(message, "empty", False):
                continue
            stats.live_messages += 1
            source_url = source_url_from_publication(message)
            if source_url is not None:
                candidates.append((message, source_url))
        print(
            f"scan progress through={message_ids[-1]} live={stats.live_messages} "
            f"source_messages={len(candidates)}"
        )

    stats.source_messages = len(candidates)
    stable_candidates: list[tuple[Message, str]] = []
    for message, source_url in candidates:
        if douyin_content_id(source_url):
            stats.stable_sources += 1
            stable_candidates.append((message, source_url))
            continue
        local_source = local_sources_by_message_id.get(message.id)
        if local_source is not None:
            stats.local_sources_reused += 1
            stable_candidates.append((message, local_source))
        else:
            stats.skipped_short_sources += 1

    thread_ids = {message.message_thread_id or 0 for message, _ in stable_candidates}
    titles = await _topic_titles(client, chat_id, thread_ids, stats)
    records_by_key: dict[str, RebuildRecord] = {}
    for message, source_url in stable_candidates:
        published_at = message.date or datetime.now(UTC)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        thread_id = message.message_thread_id or 0
        record = RebuildRecord(
            content_key=make_content_key(source_url),
            source_url=source_url,
            chat_id=chat_id,
            message_thread_id=thread_id,
            topic_title=titles.get(thread_id) or topic_title(message),
            message_id=message.id,
            message_link=safe_message_link(message),
            published_at=published_at,
        )
        if record.content_key in records_by_key:
            stats.duplicate_sources += 1
        # Scanning is ascending, so the latest still-live publication wins.
        records_by_key[record.content_key] = record

    return list(records_by_key.values()), stats


async def replace_group_records(chat_id: int, records: list[RebuildRecord]) -> tuple[int, int]:
    async with get_session() as session:
        previous = len(
            list(await session.scalars(select(MediaPublication.id).where(MediaPublication.chat_id == chat_id)))
        )
        await session.execute(delete(MediaPublication).where(MediaPublication.chat_id == chat_id))
        session.add_all(
            MediaPublication(
                content_key=record.content_key,
                source_url=record.source_url,
                chat_id=record.chat_id,
                message_thread_id=record.message_thread_id,
                topic_title=record.topic_title,
                message_id=record.message_id,
                message_link=record.message_link,
                requester_user_id=None,
                published_at=record.published_at,
            )
            for record in records
        )
    return previous, len(records)


async def rebuild(chat_id: int, max_message_id: int, *, apply: bool) -> None:
    normalized_chat_id = normalize_supergroup_id(chat_id)
    local_sources = await _local_sources_by_message_id(normalized_chat_id)
    async with Client(
        ":memory:",
        api_id=int(bs.api_id),
        api_hash=bs.api_hash,
        bot_token=bs.bot_token,
        proxy=bs.bot_proxy,
    ) as client:
        chat = await client.get_chat(normalized_chat_id)
        print(f"group id={normalized_chat_id} title={chat.title!r} max_message_id={max_message_id}")
        records, stats = await scan_group(client, normalized_chat_id, max_message_id, local_sources)

    print(
        "scan complete "
        f"scanned_ids={stats.scanned_ids} live_messages={stats.live_messages} "
        f"source_messages={stats.source_messages} unique_records={len(records)} "
        f"stable_sources={stats.stable_sources} local_sources_reused={stats.local_sources_reused} "
        f"skipped_short_sources={stats.skipped_short_sources} "
        f"duplicates_collapsed={stats.duplicate_sources} topic_failures={stats.topic_failures}"
    )
    if not apply:
        print("dry run only; pass --apply to replace this group's database rows")
        return

    previous, inserted = await replace_group_records(normalized_chat_id, records)
    print(f"database replaced chat_id={normalized_chat_id} previous={previous} inserted={inserted}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id", type=int, help="supergroup ID with or without the -100 prefix")
    parser.add_argument(
        "--max-message-id",
        type=int,
        required=True,
        help="inclusive upper message ID to scan (bots cannot discover full history)",
    )
    parser.add_argument("--apply", action="store_true", help="atomically replace this group's rows")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.max_message_id < 1:
        raise ValueError("--max-message-id must be positive")
    try:
        await rebuild(
            args.chat_id,
            args.max_message_id,
            apply=args.apply,
        )
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
