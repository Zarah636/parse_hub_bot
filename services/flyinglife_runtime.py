import asyncio
import json
import os
from pathlib import Path

from core import bs
from log import logger

logger = logger.bind(name="FlyingLifeRuntime")


class FlyingLifeRuntime:
    """Persistent process-wide preference for the optional FlyingLife engine."""

    def __init__(self, state_file: Path | None = None) -> None:
        self._state_file = state_file or (bs.config_path / "flyinglife_runtime.json")
        self._lock = asyncio.Lock()
        self._preferred = self._load()

    @property
    def preferred(self) -> bool:
        return self._preferred

    @property
    def effective(self) -> bool:
        return bs.flyinglife_enabled and self._preferred

    async def set_preferred(self, value: bool) -> bool:
        async with self._lock:
            if self._preferred == value:
                return self._preferred
            self._write(value)
            self._preferred = value
            logger.info(f"FlyingLife 全局优先策略已{'开启' if value else '关闭'}")
            return self._preferred

    def _load(self) -> bool:
        if not self._state_file.exists():
            return True
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            value = payload.get("prefer_flyinglife")
            if not isinstance(value, bool):
                raise ValueError("prefer_flyinglife must be a boolean")
            return value
        except Exception as e:
            logger.warning(f"读取 FlyingLife 运行时开关失败, 为安全起见关闭 FlyingLife: {type(e).__name__}")
            return False

    def _write(self, value: bool) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self._state_file.with_suffix(f"{self._state_file.suffix}.tmp")
        payload = {"version": 1, "prefer_flyinglife": value}
        try:
            temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_file, self._state_file)
        finally:
            temp_file.unlink(missing_ok=True)


flyinglife_runtime = FlyingLifeRuntime()
