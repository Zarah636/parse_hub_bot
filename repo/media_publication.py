from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.media_publication import MediaPublication


class MediaPublicationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, content_key: str, chat_id: int) -> MediaPublication | None:
        return cast(
            MediaPublication | None,
            await self._session.scalar(
                select(MediaPublication).where(
                    MediaPublication.content_key == content_key,
                    MediaPublication.chat_id == chat_id,
                )
            )
        )

    async def upsert(
        self,
        *,
        content_key: str,
        source_url: str,
        chat_id: int,
        message_thread_id: int,
        topic_title: str | None,
        message_id: int,
        message_link: str | None,
        requester_user_id: int | None,
        published_at: datetime,
    ) -> MediaPublication:
        publication = await self.get(
            content_key=content_key,
            chat_id=chat_id,
        )
        if publication is None:
            publication = MediaPublication(
                content_key=content_key,
                source_url=source_url,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                topic_title=topic_title,
                message_id=message_id,
                message_link=message_link,
                requester_user_id=requester_user_id,
                published_at=published_at,
            )
            self._session.add(publication)
            return publication

        publication.source_url = source_url
        publication.message_thread_id = message_thread_id
        publication.topic_title = topic_title
        publication.message_id = message_id
        publication.message_link = message_link
        publication.requester_user_id = requester_user_id
        publication.published_at = published_at
        return publication

    async def remove(self, *, content_key: str, chat_id: int) -> bool:
        publication = await self.get(content_key=content_key, chat_id=chat_id)
        if publication is None:
            return False
        await self._session.delete(publication)
        return True
