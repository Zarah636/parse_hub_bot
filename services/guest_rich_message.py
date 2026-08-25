import mimetypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from parsehub.types import AniFile, ImageFile, LivePhotoFile, VideoFile
from pyrogram import Client, raw, utils
from pyrogram.file_id import FileId, FileType, ThumbnailSource

from services.cache import CacheEntry, CacheMedia, CacheMediaType
from services.media import resolve_media_info
from services.pipeline import PipelineResult
from utils.helpers import equivalent_caption_text

MAX_RICH_MEDIA = 50


class RichLayout(StrEnum):
    SINGLE = "single"
    COLLAGE = "collage"
    SLIDESHOW = "slideshow"


class RichMediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"


class RichMessageUnsupported(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RichMediaSource:
    kind: RichMediaKind
    path: Path | None = None
    thumbnail_path: Path | None = None
    file_id: str | None = None
    width: int = 0
    height: int = 0
    duration: int = 0
    is_live: bool = False


@dataclass(frozen=True, slots=True)
class PreparedRichMedia:
    kind: RichMediaKind
    media: raw.base.InputPhoto | raw.base.InputDocument
    cache_media: CacheMedia | None = None


@dataclass(frozen=True, slots=True)
class RichMessageBuild:
    message: raw.base.InputRichMessage
    layout: RichLayout | None
    media_count: int
    cache_media: list[CacheMedia] | None


def replace_slideshow_with_collage(message: raw.base.InputRichMessage) -> raw.base.InputRichMessage | None:
    """Reuse uploaded Rich media while retrying an unsupported slideshow as a collage."""
    if not isinstance(message, raw.types.InputRichMessage):
        return None

    replaced = False
    blocks: list[raw.base.PageBlock] = []
    for block in message.blocks:
        if isinstance(block, raw.types.PageBlockSlideshow):
            blocks.append(raw.types.PageBlockCollage(items=block.items, caption=block.caption))
            replaced = True
        else:
            blocks.append(block)

    if not replaced:
        return None
    return raw.types.InputRichMessage(
        blocks=blocks,
        rtl=message.rtl,
        noautolink=message.noautolink,
        photos=message.photos,
        documents=message.documents,
        users=message.users,
    )


def choose_layout(media: list[RichMediaSource]) -> RichLayout | None:
    if not media:
        return None
    if len(media) == 1:
        return RichLayout.SINGLE

    kinds = {item.kind for item in media}
    if kinds == {RichMediaKind.PHOTO} and not any(item.is_live for item in media):
        return RichLayout.COLLAGE
    return RichLayout.SLIDESHOW


def media_sources_from_pipeline(result: PipelineResult) -> list[RichMediaSource]:
    sources: list[RichMediaSource] = []
    for processed in result.processed_list:
        source = processed.source
        if isinstance(source, LivePhotoFile):
            path = Path(source.video_path)
            width, height, duration = resolve_media_info(processed, str(path))
            sources.append(
                RichMediaSource(
                    RichMediaKind.VIDEO,
                    path=path,
                    width=width,
                    height=height,
                    duration=duration,
                    is_live=True,
                )
            )
            continue

        paths = [Path(item) for item in (processed.output_paths or [source.path])]
        for path in paths:
            width, height, duration = resolve_media_info(processed, str(path))
            if isinstance(source, ImageFile):
                kind = RichMediaKind.PHOTO
            elif isinstance(source, AniFile):
                kind = RichMediaKind.ANIMATION
            elif isinstance(source, VideoFile):
                kind = RichMediaKind.VIDEO
            else:
                raise RichMessageUnsupported(f"不支持的媒体类型: {type(source).__name__}")
            sources.append(RichMediaSource(kind, path=path, width=width, height=height, duration=duration))
    return sources


def media_sources_from_cache(entry: CacheEntry) -> list[RichMediaSource]:
    sources: list[RichMediaSource] = []
    for item in entry.media or []:
        match item.type:
            case CacheMediaType.PHOTO:
                kind = RichMediaKind.PHOTO
            case CacheMediaType.VIDEO:
                kind = RichMediaKind.VIDEO
            case CacheMediaType.ANIMATION:
                kind = RichMediaKind.ANIMATION
            case CacheMediaType.DOCUMENT:
                raise RichMessageUnsupported("通用文档缓存无法放入 Rich Message 媒体布局")
        sources.append(RichMediaSource(kind, file_id=item.file_id))
    return sources


async def build_pipeline_rich_message(
    cli: Client,
    result: PipelineResult,
    *,
    hide_title: bool = False,
    hide_desc: bool = False,
    hide_source: bool = False,
) -> RichMessageBuild:
    sources = media_sources_from_pipeline(result)
    prepared = [await prepare_rich_media(cli, item) for item in _validate_media(sources)]
    return assemble_rich_message(
        title=result.parse_result.title,
        content=result.parse_result.content,
        source_url=result.parse_result.raw_url,
        media_sources=sources,
        prepared=prepared,
        hide_title=hide_title,
        hide_desc=hide_desc,
        hide_source=hide_source,
    )


def build_cached_rich_message(
    entry: CacheEntry,
    source_url: str,
    *,
    hide_title: bool = False,
    hide_desc: bool = False,
    hide_source: bool = False,
) -> RichMessageBuild:
    sources = _validate_media(media_sources_from_cache(entry))
    prepared = [_prepare_cached_media(item) for item in sources]
    return assemble_rich_message(
        title=entry.parse_result.title,
        content=entry.parse_result.content,
        source_url=source_url,
        media_sources=sources,
        prepared=prepared,
        hide_title=hide_title,
        hide_desc=hide_desc,
        hide_source=hide_source,
    )


def assemble_rich_message(
    *,
    title: str | None,
    content: str | None,
    source_url: str,
    media_sources: list[RichMediaSource],
    prepared: list[PreparedRichMedia],
    hide_title: bool = False,
    hide_desc: bool = False,
    hide_source: bool = False,
) -> RichMessageBuild:
    if len(media_sources) != len(prepared):
        raise ValueError("media source and prepared media counts differ")

    blocks: list[raw.base.PageBlock] = []
    title = (title or "").strip()[:512]
    content = (content or "").strip()[:30000]
    if not hide_title and not hide_desc and equivalent_caption_text(title, content):
        title = ""

    layout = choose_layout(media_sources)
    # Text-only articles keep their heading/body structure. Media parsers are
    # inconsistent: FlyingLife usually provides the post copy as ``content``,
    # while ParseHub's Douyin parser provides it as ``title``. Render the first
    # visible value as an ordinary paragraph below the media.
    if title and not hide_title and layout is None:
        # Outgoing Rich Messages support section headings, but not the legacy
        # Instant View PageBlockTitle block.
        blocks.append(raw.types.PageBlockHeading1(text=raw.types.TextPlain(text=title)))
    if content and not hide_desc and layout is None:
        blocks.append(raw.types.PageBlockParagraph(text=raw.types.TextPlain(text=content)))

    empty_caption = raw.types.PageCaption(text=raw.types.TextEmpty(), credit=raw.types.TextEmpty())
    media_body = content if content and not hide_desc else title if title and not hide_title else ""
    media_blocks: list[raw.base.PageBlock] = []
    photos: list[raw.base.InputPhoto] = []
    documents: list[raw.base.InputDocument] = []
    for item in prepared:
        if item.kind == RichMediaKind.PHOTO:
            if not isinstance(item.media, raw.types.InputPhoto):
                raise TypeError("photo block requires InputPhoto")
            photos.append(item.media)
            media_blocks.append(
                raw.types.PageBlockPhoto(
                    photo_id=item.media.id,
                    caption=empty_caption,
                )
            )
        else:
            if not isinstance(item.media, raw.types.InputDocument):
                raise TypeError("video block requires InputDocument")
            documents.append(item.media)
            media_blocks.append(
                raw.types.PageBlockVideo(
                    video_id=item.media.id,
                    caption=empty_caption,
                    autoplay=item.kind == RichMediaKind.ANIMATION,
                    loop=item.kind == RichMediaKind.ANIMATION,
                )
            )

    if layout == RichLayout.SINGLE:
        blocks.extend(media_blocks)
    elif layout == RichLayout.COLLAGE:
        blocks.append(raw.types.PageBlockCollage(items=media_blocks, caption=empty_caption))
    elif layout == RichLayout.SLIDESHOW:
        blocks.append(raw.types.PageBlockSlideshow(items=media_blocks, caption=empty_caption))

    if layout is not None and media_body:
        blocks.append(raw.types.PageBlockParagraph(text=raw.types.TextPlain(text=media_body)))

    if source_url and not hide_source:
        blocks.append(
            raw.types.PageBlockFooter(
                text=raw.types.TextUrl(
                    text=raw.types.TextPlain(text="Source"),
                    url=source_url,
                    webpage_id=0,
                )
            )
        )
    if not blocks:
        blocks.append(raw.types.PageBlockParagraph(text=raw.types.TextPlain(text="-")))

    return RichMessageBuild(
        message=raw.types.InputRichMessage(
            blocks=blocks,
            photos=photos or None,
            documents=documents or None,
        ),
        layout=layout,
        media_count=len(prepared),
        cache_media=(
            [item.cache_media for item in prepared if item.cache_media is not None]
            if all(item.cache_media is not None for item in prepared)
            else None
        ),
    )


async def edit_inline_rich_message(cli: Client, inline_message_id: str, message: raw.base.InputRichMessage) -> bool:
    unpacked = utils.unpack_inline_message_id(inline_message_id)
    session = await cli.get_session(unpacked.dc_id, is_media=True)
    return cast(
        bool,
        await session.invoke(
            raw.functions.messages.EditInlineBotMessage(id=unpacked, rich_message=message),
            sleep_threshold=cli.sleep_threshold,
        ),
    )


async def delete_inline_guest_message(cli: Client, inline_message_id: str, chat_id: int | str) -> bool:
    """Delete a guest result when its inline identifier exposes a safe message ID."""
    unpacked = utils.unpack_inline_message_id(inline_message_id)
    if not isinstance(unpacked, raw.types.InputBotInlineMessageID64):
        return False

    if isinstance(chat_id, int) and utils.get_raw_peer_id(chat_id) != unpacked.owner_id:
        return False

    await cli.delete_messages(chat_id, unpacked.id)
    return True


def _validate_media(media: list[RichMediaSource]) -> list[RichMediaSource]:
    if len(media) > MAX_RICH_MEDIA:
        raise RichMessageUnsupported(f"Rich Message 最多支持 {MAX_RICH_MEDIA} 个媒体")
    return media


async def prepare_rich_media(cli: Client, item: RichMediaSource) -> PreparedRichMedia:
    """上传本地媒体并返回可复用的 Telegram 媒体引用。"""
    if item.path is None:
        raise ValueError("local rich media is missing a path")
    if item.kind == RichMediaKind.PHOTO:
        uploaded = await cli.invoke(
            raw.functions.messages.UploadMedia(
                peer=raw.types.InputPeerSelf(),
                media=raw.types.InputMediaUploadedPhoto(file=await cli.save_file(str(item.path))),
            )
        )
        if not isinstance(uploaded, raw.types.MessageMediaPhoto) or not isinstance(uploaded.photo, raw.types.Photo):
            raise RichMessageUnsupported("图片上传结果异常")
        photo = raw.types.InputPhoto(
            id=uploaded.photo.id,
            access_hash=uploaded.photo.access_hash,
            file_reference=uploaded.photo.file_reference,
        )
        photo_sizes = [
            size
            for size in uploaded.photo.sizes
            if isinstance(size, (raw.types.PhotoSize, raw.types.PhotoSizeProgressive))
        ]
        if not photo_sizes:
            raise RichMessageUnsupported("图片缺少可缓存的尺寸信息")
        main = max(photo_sizes, key=lambda size: size.w * size.h)
        file_id = FileId(
            file_type=FileType.PHOTO,
            dc_id=uploaded.photo.dc_id,
            media_id=uploaded.photo.id,
            access_hash=uploaded.photo.access_hash,
            file_reference=uploaded.photo.file_reference,
            thumbnail_source=ThumbnailSource.THUMBNAIL,
            thumbnail_file_type=FileType.PHOTO,
            thumbnail_size=main.type,
            volume_id=0,
            local_id=0,
        ).encode()
        return PreparedRichMedia(item.kind, photo, CacheMedia(type=CacheMediaType.PHOTO, file_id=file_id))

    attributes: list[raw.base.DocumentAttribute] = [
        raw.types.DocumentAttributeFilename(file_name=item.path.name),
        raw.types.DocumentAttributeVideo(
            duration=float(item.duration or 0),
            w=item.width or 0,
            h=item.height or 0,
            supports_streaming=True,
        ),
    ]
    if item.kind == RichMediaKind.ANIMATION:
        attributes.append(raw.types.DocumentAttributeAnimated())
    mime_type = mimetypes.guess_type(item.path.name)[0] or "video/mp4"
    thumbnail = (
        cast(raw.base.InputFile, await cli.save_file(str(item.thumbnail_path))) if item.thumbnail_path else None
    )
    input_media = raw.types.InputMediaUploadedDocument(
        file=await cli.save_file(str(item.path)),
        mime_type=mime_type,
        attributes=attributes,
        nosound_video=True if item.kind == RichMediaKind.ANIMATION else None,
    )
    if thumbnail is not None:
        input_media.thumb = thumbnail
    uploaded = await cli.invoke(
        raw.functions.messages.UploadMedia(
            peer=raw.types.InputPeerSelf(),
            media=input_media,
        )
    )
    if not isinstance(uploaded, raw.types.MessageMediaDocument) or not isinstance(
        uploaded.document, raw.types.Document
    ):
        raise RichMessageUnsupported("视频上传结果异常")
    document = raw.types.InputDocument(
        id=uploaded.document.id,
        access_hash=uploaded.document.access_hash,
        file_reference=uploaded.document.file_reference,
    )
    cache_type = CacheMediaType.ANIMATION if item.kind == RichMediaKind.ANIMATION else CacheMediaType.VIDEO
    file_type = FileType.ANIMATION if item.kind == RichMediaKind.ANIMATION else FileType.VIDEO
    file_id = FileId(
        file_type=file_type,
        dc_id=uploaded.document.dc_id,
        media_id=uploaded.document.id,
        access_hash=uploaded.document.access_hash,
        file_reference=uploaded.document.file_reference,
    ).encode()
    return PreparedRichMedia(
        item.kind,
        document,
        CacheMedia(type=cache_type, file_id=file_id, has_thumbnail=thumbnail is not None),
    )


# 保留旧的内部名称，避免已有调用方在公共上传函数更名后失效。
_prepare_media = prepare_rich_media


def _prepare_cached_media(item: RichMediaSource) -> PreparedRichMedia:
    if not item.file_id:
        raise ValueError("cached rich media is missing a file id")
    if item.kind == RichMediaKind.PHOTO:
        input_media = utils.get_input_media_from_file_id(item.file_id, FileType.PHOTO)
        if not isinstance(input_media, raw.types.InputMediaPhoto):
            raise RichMessageUnsupported("图片 file_id 无法转换")
        return PreparedRichMedia(
            item.kind,
            input_media.id,
            CacheMedia(type=CacheMediaType.PHOTO, file_id=item.file_id),
        )

    file_type = FileType.ANIMATION if item.kind == RichMediaKind.ANIMATION else FileType.VIDEO
    input_media = utils.get_input_media_from_file_id(item.file_id, file_type)
    if not isinstance(input_media, raw.types.InputMediaDocument):
        raise RichMessageUnsupported("视频 file_id 无法转换")
    cache_type = CacheMediaType.ANIMATION if item.kind == RichMediaKind.ANIMATION else CacheMediaType.VIDEO
    return PreparedRichMedia(item.kind, input_media.id, CacheMedia(type=cache_type, file_id=item.file_id))
