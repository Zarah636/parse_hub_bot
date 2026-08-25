import time
from dataclasses import dataclass
from pathlib import Path

from easy_ai18n import PreLocaleSelector
from parsehub.types import AnyMediaFile, DownloadResult, ProgressUnit
from parsehub.utils.media_info import MediaInfoReader

from log import logger
from utils.helpers import run_cmd, to_list
from utils.media_processing_unit import MediaProcessingUnit


@dataclass
class ProcessedMedia:
    source: AnyMediaFile
    output_paths: list[Path] | None = None
    output_dir: Path | None = None


def resolve_media_info(processed: ProcessedMedia, file_path: str) -> tuple[int, int, int]:
    """获取媒体的宽、高、时长。若经过转码则从文件读取，否则使用源信息。"""
    if processed.output_paths:
        info = MediaInfoReader.read(file_path)
        return info.width, info.height, info.duration
    return processed.source.width, processed.source.height, getattr(processed.source, "duration", 0)


async def create_video_thumbnail(file_path: str | Path, duration: int = 0) -> Path | None:
    """从已下载的视频截取 Telegram 可内嵌的 JPEG thumbnail。"""
    source = Path(file_path)
    output = source.parent / f".{source.stem}_inline_thumb_{time.time_ns()}.jpg"
    timestamp = min(max(duration * 0.03, 0.1), max(duration - 0.1, 0.1), 10.0) if duration else 1.0

    try:
        for quality in (4, 10):
            await run_cmd(
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease",
                "-q:v",
                str(quality),
                "-y",
                str(output),
                timeout=60,
            )
            if output.is_file() and 0 < output.stat().st_size <= 200 * 1024:
                logger.debug(f"内联视频 thumbnail 已生成: path={output}, timestamp={timestamp:.3f}s")
                return output
    except Exception as e:
        logger.warning(f"内联视频 thumbnail 生成失败: {type(e).__name__}: {e}")

    output.unlink(missing_ok=True)
    return None


def progress(current: int, total: int, unit: ProgressUnit, _t: PreLocaleSelector) -> str | None:
    if unit == "bytes":
        if total <= 0:
            return None

        text = _t(f"下 载 中... | {current * 100 / total:.0f}%")
        if round(current * 100 / total, 1) % 25 == 0:
            return str(text)
    else:
        text = _t(f"下 载 中... | {current}/{total}")
        if (current + 1) % 3 == 0 or (current + 1) == total:
            return str(text)
    return None


async def process_media_files(download_result: DownloadResult) -> list[ProcessedMedia]:
    """对下载结果中的媒体文件进行处理，返回 ProcessedMedia 列表"""
    processed_dir = download_result.output_dir.joinpath("processed")
    processor = MediaProcessingUnit(processed_dir, segment_height=1920, logger=logger.bind(name="MediaProcessor").debug)
    media_files = to_list(download_result.media)
    logger.debug(f"开始媒体处理: 文件数={len(media_files)}, output_dir={processed_dir}")
    processed_list: list[ProcessedMedia] = []
    for media_file in media_files:
        # 对于实况图片只处理图片, 不处理视频
        logger.debug(f"处理文件: {media_file.path}")
        result = await processor.process(media_file.path)
        logger.debug(f"处理结果: output_paths={result.output_paths}")
        processed_list.append(ProcessedMedia(media_file, result.output_paths, result.temp_dir))
    logger.debug(f"媒体处理完成: 处理数={len(processed_list)}")
    return processed_list
