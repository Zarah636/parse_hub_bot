import asyncio
import hashlib
import json
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import aiofiles
import httpx
from easy_ai18n import PreLocaleSelector
from parsehub import DownloadResult
from parsehub.types import (
    AnyParseResult,
    ImageFile,
    ImageParseResult,
    ImageRef,
    ProgressUnit,
    VideoFile,
    VideoParseResult,
    VideoRef,
)
from pydantic import BaseModel, Field, field_validator

from core import bs
from log import logger
from services.media import ProcessedMedia, process_media_files

logger = logger.bind(name="FlyingLife")

FLYINGLIFE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class FlyingLifeError(Exception):
    """FlyingLife 统一错误。消息中不得包含会话或签名 URL。"""


class FlyingLifeAuthError(FlyingLifeError):
    pass


class FlyingLifeParseError(FlyingLifeError):
    pass


class FlyingLifeDownloadError(FlyingLifeError):
    pass


class FlyingLifeUnavailable(FlyingLifeError):
    pass


class FlyingLifeReporter(Protocol):
    async def report(self, text: str) -> None: ...


class FlyingLifePayload(BaseModel):
    title: str = ""
    text: str = ""
    images: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    video: str | None = None
    musics: list[str] = Field(default_factory=list)
    music: str | None = None

    @field_validator("title", "text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("images", "videos", "musics", mode="before")
    @classmethod
    def normalize_media_list(cls, value: Any) -> Any:
        """Web API may return null or a scalar for optional media collections."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @property
    def video_list(self) -> list[str]:
        return self.videos or ([self.video] if self.video else [])

    @property
    def music_list(self) -> list[str]:
        return self.musics or ([self.music] if self.music else [])


@dataclass
class FlyingLifeRunResult:
    parse_result: AnyParseResult
    processed_list: list[ProcessedMedia] = field(default_factory=list)
    output_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class FlyingLifeInlineCandidate:
    """Pair the public inline preview with the authenticated proxy download result."""

    preview_result: AnyParseResult
    download_result: AnyParseResult


ProgressCallback = Callable[[int, int, ProgressUnit], Awaitable[None]]


class FlyingLifeService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(bs.flyinglife_concurrency)
        self._session_id: str | None = None
        self._session_fingerprint: str | None = None
        self._auth_blocked = False
        self._failures = 0
        self._open_until = 0.0

    @property
    def auth_file(self) -> Path:
        return bs.config_path / "flyinglife_auth.json"

    @property
    def base_url(self) -> str:
        return bs.flyinglife_base_url.rstrip("/")

    def _load_session(self) -> str | None:
        if bs.flyinglife_session_id:
            return bs.flyinglife_session_id.get_secret_value().strip() or None

        if not self.auth_file.exists():
            return None
        try:
            data = json.loads(self.auth_file.read_text(encoding="utf-8"))
            value = data.get("session_id")
            return value.strip() if isinstance(value, str) and value.strip() else None
        except Exception as e:
            logger.warning(f"读取 FlyingLife 认证文件失败: {type(e).__name__}")
            return None

    def _session_source_fingerprint(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        source_version = "environment"
        if not bs.flyinglife_session_id:
            try:
                source_version = f"file:{self.auth_file.stat().st_mtime_ns}"
            except OSError:
                source_version = "file:unknown"
        value = f"{source_version}\0{session_id}".encode()
        return hashlib.sha256(value).hexdigest()

    def _refresh_session(self) -> None:
        session_id = self._load_session()
        fingerprint = self._session_source_fingerprint(session_id)
        if fingerprint == self._session_fingerprint:
            return
        self._session_id = session_id
        self._session_fingerprint = fingerprint
        self._auth_blocked = False
        self._failures = 0
        self._open_until = 0.0
        logger.info("FlyingLife 会话已加载" if session_id else "FlyingLife 未配置会话")

    def can_attempt(self, platform_id: str) -> bool:
        if not bs.flyinglife_enabled:
            return False
        if platform_id.lower() not in bs.flyinglife_platform_id_set:
            return False

        self._refresh_session()
        if not self._session_id or self._auth_blocked:
            return False
        if time.monotonic() < self._open_until:
            return False
        return True

    async def initialize(self) -> None:
        if not bs.flyinglife_enabled:
            return
        self._refresh_session()
        if not self._session_id:
            logger.warning("FlyingLife 已启用但没有可用会话, 请运行认证向导")
            return
        try:
            await self.check_login()
        except FlyingLifeError as e:
            self._auth_blocked = isinstance(e, FlyingLifeAuthError)
            logger.warning(f"FlyingLife 启动验证失败: {e}")
        else:
            logger.success("FlyingLife 登录状态验证成功")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=min(bs.flyinglife_download_timeout, 15),
                read=bs.flyinglife_download_timeout,
                write=bs.flyinglife_download_timeout,
                pool=bs.flyinglife_download_timeout,
            )
            self._client = httpx.AsyncClient(
                # API/Proxy 不应跨站重定向，避免将 X-Session-Id 带给 CDN。
                follow_redirects=False,
                timeout=timeout,
                headers={
                    "User-Agent": FLYINGLIFE_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        if not self._session_id:
            raise FlyingLifeAuthError("登录状态缺失")
        return {"X-Session-Id": self._session_id}

    async def _api_request(
        self,
        path: str,
        *,
        method: str = "POST",
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        client = self._get_client()
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        try:
            async with asyncio.timeout(timeout or bs.flyinglife_parse_timeout):
                response = await client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body or {})
        except TimeoutError as e:
            raise FlyingLifeUnavailable("接口请求超时") from e
        except httpx.HTTPError as e:
            raise FlyingLifeUnavailable(f"接口网络错误: {type(e).__name__}") from e

        try:
            payload = response.json()
        except Exception as e:
            raise FlyingLifeUnavailable("接口返回了非 JSON 内容") from e

        code = payload.get("code") if isinstance(payload, dict) else None
        if code == 401:
            raise FlyingLifeAuthError("登录状态已失效")
        if response.status_code >= 300:
            raise FlyingLifeUnavailable(f"接口 HTTP {response.status_code}")
        if code not in (0, 200):
            message = payload.get("message") if isinstance(payload, dict) else None
            raise FlyingLifeParseError(self._sanitize_error(message, code))
        return payload.get("data")

    def _sanitize_error(self, message: Any, code: Any) -> str:
        text = str(message or f"接口返回错误 code={code}")
        text = re.sub(r"https?://\S+", "<url>", text)
        if self._session_id:
            text = text.replace(self._session_id, "<session>")
        return text[:300]

    async def check_login(self) -> dict[str, Any]:
        data = await self._api_request("/api/check_login.php")
        if not isinstance(data, dict):
            raise FlyingLifeAuthError("登录状态返回格式异常")
        return data

    def _proxy_url(self, media_url: str, source_url: str, media_type: str) -> str:
        query = urlencode({"url": media_url, "referer": source_url, "type": media_type})
        return f"{self.base_url}/api/media_proxy.php?{query}"

    async def _fetch_payload(self, url: str, *, timeout: float | None = None) -> FlyingLifePayload:
        data = await self._api_request("/api/parse.php", json_body={"url": url}, timeout=timeout)
        try:
            return FlyingLifePayload.model_validate(data)
        except Exception as e:
            raise FlyingLifeParseError("解析结果格式异常") from e

    def _build_result(
        self,
        payload: FlyingLifePayload,
        url: str,
        raw_url: str,
        *,
        proxy_media: bool,
    ) -> AnyParseResult:
        if payload.music_list:
            raise FlyingLifeParseError("第一版暂不支持音频结果")

        def media_url(value: str, media_type: str) -> str:
            return self._proxy_url(value, url, media_type) if proxy_media else value

        videos = payload.video_list
        if videos:
            if len(videos) != 1:
                raise FlyingLifeParseError("第一版暂不支持多视频结果")
            cover_url = media_url(payload.images[0], "image") if payload.images else None
            video_ref = VideoRef(url=media_url(videos[0], "video"), thumb_url=cover_url)
            result: AnyParseResult = VideoParseResult(title=payload.title, content=payload.text, video=video_ref)
        elif payload.images:
            image_refs = [ImageRef(url=media_url(item, "image")) for item in payload.images]
            result = ImageParseResult(title=payload.title, content=payload.text, photo=image_refs)
        else:
            raise FlyingLifeParseError("没有解析到可用媒体")

        result.raw_url = raw_url
        return result

    async def parse(self, url: str, raw_url: str) -> AnyParseResult:
        payload = await self._fetch_payload(url)
        return self._build_result(payload, url, raw_url, proxy_media=True)

    async def parse_inline_candidate(self, url: str, raw_url: str) -> FlyingLifeInlineCandidate:
        """Parse once and keep public CDN preview URLs separate from authenticated proxy URLs."""
        if time.monotonic() < self._open_until:
            raise FlyingLifeUnavailable("远程解析处于熔断冷却期")

        async with self._semaphore:
            try:
                payload = await self._fetch_payload(url, timeout=bs.flyinglife_inline_parse_timeout)
                preview_result = self._build_result(payload, url, raw_url, proxy_media=False)
                download_result = self._build_result(payload, url, raw_url, proxy_media=True)
            except BaseException as e:
                if isinstance(e, Exception):
                    self._record_failure(e)
                raise

        self._record_success()
        return FlyingLifeInlineCandidate(preview_result=preview_result, download_result=download_result)

    async def _download_proxy(
        self,
        url: str,
        target: Path,
        media_type: str,
        progress: ProgressCallback | None = None,
    ) -> None:
        client = self._get_client()
        headers = {
            **self._auth_headers(),
            "Accept": "video/*,image/*,*/*;q=0.8",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "video" if media_type == "video" else "image",
        }
        try:
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                if response.status_code >= 300:
                    raise FlyingLifeDownloadError(f"代理下载 HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise FlyingLifeDownloadError("代理返回了非媒体内容")
                total = int(response.headers.get("content-length", "0") or 0)
                if total > bs.flyinglife_max_media_bytes:
                    raise FlyingLifeDownloadError("媒体超过配置的大小上限")

                current = 0
                async with aiofiles.open(target, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        current += len(chunk)
                        if current > bs.flyinglife_max_media_bytes:
                            raise FlyingLifeDownloadError("媒体超过配置的大小上限")
                        await f.write(chunk)
                        if progress and total > 0:
                            await progress(current, total, "bytes")
        except FlyingLifeDownloadError:
            raise
        except httpx.HTTPError as e:
            raise FlyingLifeDownloadError(f"代理下载网络错误: {type(e).__name__}") from e
        except OSError as e:
            raise FlyingLifeDownloadError(f"代理下载写入错误: {type(e).__name__}") from e

        if not target.exists() or target.stat().st_size == 0:
            raise FlyingLifeDownloadError("代理返回了空文件")

    async def download(
        self,
        parse_result: AnyParseResult,
        progress: ProgressCallback | None = None,
        *,
        download_video_cover: bool = True,
        save_metadata: bool = False,
    ) -> DownloadResult:
        digest = hashlib.sha256(parse_result.raw_url.encode()).hexdigest()[:12]
        output_dir = bs.download_dir / f"flyinglife_{digest}_{time.time_ns()}"
        output_dir.mkdir(parents=True, exist_ok=False)
        media_refs = list(parse_result.media) if isinstance(parse_result.media, Sequence) else [parse_result.media]
        media_refs = [item for item in media_refs if item is not None]
        files: list[ImageFile | VideoFile] = []

        try:
            cover_url = media_refs[0].thumb_url if media_refs and isinstance(media_refs[0], VideoRef) else None
            if download_video_cover and cover_url:
                cover_path = output_dir / "cover.jpg"
                try:
                    await self._download_proxy(cover_url, cover_path, "image")
                except FlyingLifeDownloadError as e:
                    logger.warning(f"FlyingLife 视频封面下载失败, 继续处理视频: {e}")
                    media_refs[0].thumb_url = None
                else:
                    media_refs[0].thumb_url = str(cover_path)

            for index, media_ref in enumerate(media_refs, start=1):
                if isinstance(media_ref, ImageRef):
                    target = output_dir / f"{index:03d}.jpg"
                    await self._download_proxy(media_ref.url, target, "image")
                    files.append(ImageFile(path=target))
                elif isinstance(media_ref, VideoRef):
                    target = output_dir / f"{index:03d}.mp4"
                    callback = progress if len(media_refs) == 1 else None
                    await self._download_proxy(media_ref.url, target, "video", callback)
                    files.append(VideoFile(path=target))
                else:
                    raise FlyingLifeDownloadError("FlyingLife 返回了不支持的媒体类型")

                if progress and len(media_refs) > 1:
                    await progress(len(files), len(media_refs), "count")

            if save_metadata:
                async with aiofiles.open(output_dir / "metadata.json", "w", encoding="utf-8") as f:
                    await f.write(json.dumps(parse_result.to_dict(), ensure_ascii=False, indent=2))
        except BaseException:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise

        media: ImageFile | VideoFile | Sequence[ImageFile | VideoFile]
        media = files[0] if len(files) == 1 else files
        return DownloadResult(media=media, output_dir=output_dir)

    async def parse_only(
        self,
        url: str,
        raw_url: str,
        reporter: FlyingLifeReporter,
        *,
        _t: PreLocaleSelector,
    ) -> AnyParseResult:
        """Parse metadata without starting the media proxy download."""
        if time.monotonic() < self._open_until:
            raise FlyingLifeUnavailable("远程解析处于熔断冷却期")

        async with self._semaphore:
            try:
                await reporter.report(_t("解 析 中..."))
                parse_result = await self.parse(url, raw_url)
            except BaseException as e:
                if isinstance(e, Exception):
                    self._record_failure(e)
                raise

        self._record_success()
        return parse_result

    async def run(
        self,
        url: str,
        raw_url: str,
        reporter: FlyingLifeReporter,
        *,
        skip_media_processing: bool,
        download_video_cover: bool,
        save_metadata: bool,
        _t: PreLocaleSelector,
        prepared_result: AnyParseResult | None = None,
    ) -> FlyingLifeRunResult:
        if time.monotonic() < self._open_until:
            raise FlyingLifeUnavailable("远程解析处于熔断冷却期")

        async with self._semaphore:
            download_result: DownloadResult | None = None
            try:
                if prepared_result is None:
                    await reporter.report(_t("解 析 中..."))
                    parse_result = await self.parse(url, raw_url)
                else:
                    parse_result = prepared_result
                await reporter.report(_t("下 载 中..."))

                async def progress(current: int, total: int, unit: str) -> None:
                    from services.media import progress as format_progress

                    text = format_progress(current, total, unit, _t)
                    if text:
                        await reporter.report(text)

                download_result = await self.download(
                    parse_result,
                    progress,
                    download_video_cover=download_video_cover,
                    save_metadata=save_metadata,
                )
                if skip_media_processing:
                    processed_list = [
                        ProcessedMedia(item, [Path(item.path)])
                        for item in (
                            download_result.media
                            if isinstance(download_result.media, Sequence)
                            else [download_result.media]
                        )
                    ]
                else:
                    processed_list = await process_media_files(download_result)
            except BaseException as e:
                if download_result is not None:
                    shutil.rmtree(download_result.output_dir, ignore_errors=True)
                if isinstance(e, Exception):
                    self._record_failure(e)
                raise

        self._record_success()
        return FlyingLifeRunResult(
            parse_result=parse_result,
            processed_list=processed_list,
            output_dir=download_result.output_dir,
        )

    def _record_failure(self, error: Exception) -> None:
        if isinstance(error, FlyingLifeAuthError):
            self._auth_blocked = True
            logger.warning("FlyingLife 登录状态失效, 后续请求将使用 ParseHub")
            return
        if isinstance(error, FlyingLifeParseError):
            return
        self._failures += 1
        if self._failures >= bs.flyinglife_failure_threshold:
            self._open_until = time.monotonic() + bs.flyinglife_cooldown
            logger.warning(f"FlyingLife 连续失败 {self._failures} 次, 已进入熔断冷却")

    def _record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0


flyinglife = FlyingLifeService()
