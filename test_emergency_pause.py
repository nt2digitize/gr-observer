import os
import unittest
from unittest.mock import AsyncMock

from gr_observer.emergency_pause import EmergencyPvPauseMixin


class _Item:
    enabled = True
    reason = "Ligado"


class _Registry:
    def __init__(self):
        self.item = _Item()

    def get(self, module_id):
        assert module_id == "pv_reply"
        return self.item


class _Base:
    def __init__(self):
        self.registry = _Registry()
        self.storage = type("Storage", (), {"set_module_state": AsyncMock()})()

    async def apply_startup_requests(self):
        return None


class _Runtime(EmergencyPvPauseMixin, _Base):
    pass


class EmergencyPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_flag_forces_pv_off_without_deleting_work(self):
        old = os.environ.get("PV_REPLY_EMERGENCY_PAUSE")
        os.environ["PV_REPLY_EMERGENCY_PAUSE"] = "1"
        try:
            runtime = _Runtime()
            await runtime.apply_startup_requests()
            self.assertFalse(runtime.registry.item.enabled)
            self.assertIn("contenção", runtime.registry.item.reason)
            runtime.storage.set_module_state.assert_awaited_once_with(
                "pv_reply", False, "Pausado por contenção de segurança do PV"
            )
        finally:
            if old is None:
                os.environ.pop("PV_REPLY_EMERGENCY_PAUSE", None)
            else:
                os.environ["PV_REPLY_EMERGENCY_PAUSE"] = old


if __name__ == "__main__":
    unittest.main()
