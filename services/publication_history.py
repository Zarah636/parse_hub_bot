from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pyrogram.enums import ChatType
from pyrogram.types import Message

from db import get_session
from repo.media_publication import MediaPublicationRepo
from services.cache import TTLCache

_DOUYIN_CONTENT_RE = re.compile(r"/(?:share/)?(?:video|note)/(\d+)(?:/|$)")
_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM}


def normalize_source_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value.rstrip("/")

    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def make_content_key(url: str) -> str:
    normalized = normalize_source_url(url)
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    is_douyin = host == "douyin.com" or host.endswith(".douyin.com")
    is_iesdouyin = host == "iesdouyin.com" or host.endswith(".iesdouyin.com")
    if is_douyin or is_iesdouyin:
        match = _DOUYIN_CONTENT_RE.search(parts.path)
        if match:
            normalized = f"douyin:{match.group(1)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def message_scope(message: Message) -> tuple[int, int] | None:
    chat = message.chat
    if not chat or chat.type not in _GROUP_TYPES:
        return None
    chat_id = chat.id
    if chat_id is None:
        return None
    return chat_id, message.message_thread_id or 0


def topic_title(message: Message) -> str | None:
    topic = getattr(message, "topic", None)
    title = getattr(topic, "title", None)
    return str(title) if title else None


def safe_message_link(message: Message) -> str | None:
    if not message.chat or message.chat.type not in {ChatType.SUPERGROUP, ChatType.FORUM}:
        return None
    try:
        return message.link or None
    except (AttributeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    source_url: str
    chat_id: int
    message_thread_id: int
    topic_title: str | None
    message_id: int
    message_link: str | None
    requester_user_id: int | None
    published_at: datetime


class PublicationHistory:
    async def find(self, source_url: str, message: Message) -> PublicationRecord | None:
        scope = message_scope(message)
        if scope is None:
            return None
        chat_id, _ = scope
        async with get_session() as session:
            publication = await MediaPublicationRepo(session).get(
                content_key=make_content_key(source_url),
                chat_id=chat_id,
            )
            if publication is None:
                return None
            return PublicationRecord(
                source_url=publication.source_url,
                chat_id=publication.chat_id,
                message_thread_id=publication.message_thread_id,
                topic_title=publication.topic_title,
                message_id=publication.message_id,
                message_link=publication.message_link,
                requester_user_id=publication.requester_user_id,
                published_at=publication.published_at,
            )

    async def record(
        self,
        source_url: str,
        request_message: Message,
        sent_messages: list[Message],
        *,
        requester_user_id: int | None,
    ) -> PublicationRecord | None:
        scope = message_scope(request_message)
        if scope is None or not sent_messages:
            return None

        chat_id, message_thread_id = scope
        sent_message = sent_messages[0]
        published_at = sent_message.date or datetime.now(UTC)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        title = topic_title(sent_message) or topic_title(request_message)
        link = safe_message_link(sent_message)

        async with get_session() as session:
            await MediaPublicationRepo(session).upsert(
                content_key=make_content_key(source_url),
                source_url=normalize_source_url(source_url),
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                topic_title=title,
                message_id=sent_message.id,
                message_link=link,
                requester_user_id=requester_user_id,
                published_at=published_at,
            )

        return PublicationRecord(
            source_url=normalize_source_url(source_url),
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            topic_title=title,
            message_id=sent_message.id,
            message_link=link,
            requester_user_id=requester_user_id,
            published_at=published_at,
        )


@dataclass(frozen=True, slots=True)
class PendingDuplicateConfirmation:
    url: str
    mode: str
    user_id: int
    chat_id: int
    message_thread_id: int


class DuplicateConfirmationStore:
    def __init__(self, ttl: float = 10 * 60, maxsize: int = 1000) -> None:
        self._cache = TTLCache(ttl=ttl, maxsize=maxsize)

    async def create(self, pending: PendingDuplicateConfirmation) -> str:
        token = secrets.token_urlsafe(9)
        await self._cache.set(token, pending)
        return token

    async def get(self, token: str) -> PendingDuplicateConfirmation | None:
        value = await self._cache.get(token)
        return value if isinstance(value, PendingDuplicateConfirmation) else None

    async def pop(self, token: str) -> PendingDuplicateConfirmation | None:
        value = await self._cache.pop(token)
        return value if isinstance(value, PendingDuplicateConfirmation) else None


publication_history = PublicationHistory()
duplicate_confirmations = DuplicateConfirmationStore()
