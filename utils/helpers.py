import asyncio
import functools
import shutil
import tarfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from log import logger


def clear_directory_contents(dir_path: str | Path) -> None:
    """Remove children of a working directory while preserving the directory itself.

    Keeping the root is important when it is a Docker bind mount. Mounted child
    directories are skipped so shutdown cleanup cannot cross mount boundaries.
    """
    root = Path(dir_path)
    if not root.exists():
        return
    if not root.is_dir():
        logger.warning(f"跳过清理非目录路径: {root}")
        return

    for child in root.iterdir():
        try:
            if child.is_mount():
                logger.warning(f"跳过清理挂载点: {child}")
            elif child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"清理下载残留失败: path={child}, error={type(e).__name__}")


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
