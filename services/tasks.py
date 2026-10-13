"""Bound heavy requests across engines and drain them before resource shutdown."""

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from inspect import iscoroutine
from typing import Any

from core import bs
from log import logger

_slots = asyncio.Semaphore(bs.parse_concurrency)
_active: set[asyncio.Task[Any]] = set()
_closing = False
_pending_urls: set[str] = set()


@contextmanager
def claim_url(url: str, *, enabled: bool = True) -> Iterator[bool]:
    """Claim a canonical URL before waiting for a heavy request slot."""
    if not enabled:
        yield True
        return
    if url in _pending_urls:
        yield False
        return
    _pending_urls.add(url)
    try:
        yield True
    finally:
        _pending_urls.discard(url)


def start() -> None:
    global _closing, _slots
    _closing = False
    _slots = asyncio.Semaphore(bs.parse_concurrency)


def heavy_request[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    @functools.wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if _closing:
            return None
        task = asyncio.current_task()
        already_tracked = task in _active
        if task is not None:
            _active.add(task)
        try:
            async with _slots:
                if _closing:
                    return None
                return await fn(*args, **kwargs)
        finally:
            if task is not None and not already_tracked:
                _active.discard(task)

    return wrapped  # type: ignore[return-value]


def spawn(coro: Awaitable[None]) -> None:
    if _closing:
        if iscoroutine(coro):
            coro.close()
        return
    task = asyncio.ensure_future(coro)
    _active.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _active.discard(completed)
        if not completed.cancelled() and (exc := completed.exception()) is not None:
            logger.opt(exception=exc).error("后台解析任务异常")

    task.add_done_callback(done)


async def shutdown() -> None:
    global _closing
    _closing = True
    tasks = _active - {asyncio.current_task()}
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=bs.parse_shutdown_timeout)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
