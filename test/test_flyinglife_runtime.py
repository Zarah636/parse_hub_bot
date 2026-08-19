import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")

from services.flyinglife import FlyingLifeService  # noqa: E402
from services.flyinglife_runtime import FlyingLifeRuntime  # noqa: E402


class FlyingLifeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_defaults_to_enabled_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "flyinglife_runtime.json"
            runtime = FlyingLifeRuntime(state_file)

            self.assertTrue(runtime.preferred)
            await runtime.set_preferred(False)

            self.assertFalse(runtime.preferred)
            self.assertFalse(json.loads(state_file.read_text(encoding="utf-8"))["prefer_flyinglife"])
            self.assertFalse(FlyingLifeRuntime(state_file).preferred)

    async def test_invalid_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "flyinglife_runtime.json"
            state_file.write_text('{"prefer_flyinglife": "no"}', encoding="utf-8")

            self.assertFalse(FlyingLifeRuntime(state_file).preferred)

    async def test_disabled_policy_does_not_touch_auth_or_breaker_state(self) -> None:
        service = FlyingLifeService()
        service._refresh_session = MagicMock()  # type: ignore[method-assign]

        with patch("services.flyinglife.flyinglife_runtime") as runtime:
            runtime.effective = False
            self.assertFalse(service.should_attempt("douyin", context="test"))

        service._refresh_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
