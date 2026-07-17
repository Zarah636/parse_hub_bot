from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MediaPublication(Base):
    __tablename__ = "media_publications"
    __table_args__ = (
        UniqueConstraint(
            "content_key",
            "chat_id",
            name="uq_media_publication_chat",
        ),
        Index("ix_media_publication_chat", "chat_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    content_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    topic_title: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_link: Mapped[str | None] = mapped_column(Text)
    requester_user_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
