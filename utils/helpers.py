import asyncio
import functools
import re
import tarfile
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from log import logger


def equivalent_caption_text(left: str | None, right: str | None) -> bool:
    """判断标题与正文是否仅差异于排版或数字 ID 前缀。"""

    def normalize(value: str | None) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return "".join(char for char in normalized if char.isalnum())

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True

    # Some parsers prepend a numeric post ID (for example ``21123_``) to
    # an otherwise equivalent title. Require a separator so legitimate
    # titles such as ``2024夏日`` are not treated as duplicates of ``夏日``.
    left_without_id = re.sub(
        r"^\d+[\W_]+",
        "",
        unicodedata.normalize("NFKC", left or "").casefold(),
        count=1,
    )
    return bool(left_without_id and normalize(left_without_id) == normalized_right)


async def run_cmd(*cmd: str, timeout: float = 30) -> str:
    """运行外部命令并异步读取输出"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return stdout.decode().strip()


def to_list[T](v: T | Sequence[T]) -> Sequence[T]:
    return v if isinstance(v, Sequence) else [v]


def pack_dir_to_tar_gz(dir_path: str | Path, output_path: str | Path | None = None) -> Path:
    """
    将目录打包为 tar.gz，返回压缩包路径。

    Args:
        dir_path: 要打包的目录
        output_path: 输出压缩包路径；不传则默认生成同名 .tar.gz

    Returns:
        生成的 tar.gz 文件路径
    """
    source_dir = Path(dir_path).resolve()
    if not source_dir.is_dir():
        raise ValueError(f"不是有效目录: {source_dir}")

    if output_path is None:
        output_path = source_dir.with_suffix(".tar.gz")
    else:
        output_path = Path(output_path).resolve()

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)

    return output_path


def with_request_id[T](func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        request_id = str(uuid.uuid4())[:8]
        with logger.contextualize(req_id=request_id):
            return await func(*args, **kwargs)

    return wrapper


def mask_secret(value: str) -> str:
    if not value:
        return ""
    c = min(len(value) // 3, 4)
    return f"{value[:c]}******{value[-c:]}"
