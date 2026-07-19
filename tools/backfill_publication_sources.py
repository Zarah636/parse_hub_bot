"""One-time migration: ``python -m tools.backfill_publication_sources``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pyrogram import Client, enums
from pyrogram.types import Message
from sqlalchemy import select

from core import bs
from db import get_session
from db.engine import close_db
from db.models.media_publication import MediaPublication
from services.cache import persistent_cache
from services.parser import ParseService
from services.publication_history import (
    douyin_content_id,
    make_content_key,
    normalize_source_url,
    safe_message_link,
    topic_title,
)


@dataclass(frozen=True, slots=True)
class PublicationSnapshot:
    id: int
    source_url: str
    chat_id: int
    message_thread_id: int
    message_id: int


@dataclass(slots=True)
class BackfillStats:
    scanned: int = 0
    migrated: int = 0
    merged: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    cache_updated: int = 0


def source_url_from_message(message: Message) -> str | None:
    entities = message.caption_entities or message.entities or []
    for entity in reversed(entities):
        if entity.type != enums.MessageEntityType.TEXT_LINK or not entity.url:
            continue
        if douyin_content_id(entity.url):
            return normalize_source_url(entity.url)
    return None


async def _snapshots() -> list[PublicationSnapshot]:
    async with get_session() as session:
        publications = list(await session.scalars(select(MediaPublication).order_by(MediaPublication.id)))
    return [
        PublicationSnapshot(
            id=publication.id,
            source_url=publication.source_url,
            chat_id=publication.chat_id,
            message_thread_id=publication.message_thread_id,
            message_id=publication.message_id,
        )
        for publication in publications
    ]


async def _remove_by_id(publication_id: int) -> bool:
    async with get_session() as session:
        publication = await session.get(MediaPublication, publication_id)
        if publication is None:
            return False
        await session.delete(publication)
        return True


async def _update_publication(
    snapshot: PublicationSnapshot,
    canonical_url: str,
    message: Message,
    live_topic_title: str | None,
) -> str:
    content_key = make_content_key(canonical_url)
    async with get_session() as session:
        publication = await session.get(MediaPublication, snapshot.id)
        if publication is None:
            return "skipped"

        existing = await session.scalar(
            select(MediaPublication).where(
                MediaPublication.content_key == content_key,
                MediaPublication.chat_id == snapshot.chat_id,
            )
        )
        current_is_latest = existing is None or publication.published_at >= existing.published_at
        if existing is not None and existing.id != publication.id:
            if current_is_latest:
                existing.source_url = canonical_url
                existing.message_thread_id = publication.message_thread_id
                existing.topic_title = live_topic_title or publication.topic_title
                existing.message_id = publication.message_id
                existing.message_link = safe_message_link(message) or publication.message_link
                existing.requester_user_id = publication.requester_user_id
                existing.published_at = publication.published_at
            await session.delete(publication)
            return "merged"

        publication.content_key = content_key
        publication.source_url = canonical_url
        publication.topic_title = live_topic_title or publication.topic_title
        publication.message_link = safe_message_link(message) or publication.message_link
        return "migrated"


async def _update_cache(short_url: str, canonical_url: str) -> bool:
    entry = await persistent_cache.get(short_url)
    if entry is None:
        return False
    entry.parse_result.raw_url = canonical_url
    await persistent_cache.set(short_url, entry)
    return True


async def backfill() -> BackfillStats:
    stats = BackfillStats()
    snapshots = await _snapshots()
    async with Client(
        ":memory:",
        api_id=int(bs.api_id),
        api_hash=bs.api_hash,
        bot_token=bs.bot_token,
        proxy=bs.bot_proxy,
    ) as client:
        for snapshot in snapshots:
            stats.scanned += 1
            try:
                message = await client.get_messages(snapshot.chat_id, snapshot.message_id, replies=0)
            except Exception as error:
                stats.failed += 1
                print(
                    f"failed id={snapshot.id} message_id={snapshot.message_id} "
                    f"error={type(error).__name__}"
                )
                continue

            if message is None or getattr(message, "empty", False):
                if await _remove_by_id(snapshot.id):
                    stats.deleted += 1
                continue

            canonical_url = source_url_from_message(message)
            if canonical_url is None:
                try:
                    resolved_url = await ParseService().get_raw_url(snapshot.source_url)
                except Exception:
                    stats.skipped += 1
                    continue
                canonical_url = normalize_source_url(resolved_url) if douyin_content_id(resolved_url) else None
            if canonical_url is None:
                stats.skipped += 1
                continue

            live_topic_title = topic_title(message)
            if snapshot.message_thread_id:
                try:
                    topic = await client.get_forum_topics_by_id(snapshot.chat_id, snapshot.message_thread_id)
                except Exception:
                    pass
                else:
                    live_topic_title = getattr(topic, "title", None) or live_topic_title

            outcome = await _update_publication(snapshot, canonical_url, message, live_topic_title)
            if outcome == "migrated":
                stats.migrated += 1
            elif outcome == "merged":
                stats.merged += 1
            else:
                stats.skipped += 1

            if await _update_cache(snapshot.source_url, canonical_url):
                stats.cache_updated += 1

    return stats


async def main() -> None:
    try:
        stats = await backfill()
        print(
            "backfill complete "
            f"scanned={stats.scanned} migrated={stats.migrated} merged={stats.merged} "
            f"deleted={stats.deleted} skipped={stats.skipped} failed={stats.failed} "
            f"cache_updated={stats.cache_updated}"
        )
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
