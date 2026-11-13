"""Direct teleport routing without game memory, equipment or state readers."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from wizwalker import XYZ
from wizwalker.client import Client


class DirectTeleportTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_request_is_one_native_call_with_exact_options_and_yaw(self):
        client = Client.__new__(Client)
        client.client_object = SimpleNamespace(read_base_address=AsyncMock(return_value=123))
        client.body = SimpleNamespace(write_yaw=AsyncMock())
        client._teleport_object = AsyncMock()
        target = XYZ(9000, 200, 40)
        await client.teleport(target, yaw=1.2, wait_on_inuse=False,
                              wait_on_inuse_timeout=2, purge_on_after_unuser_fixer=False,
                              purge_on_after_unuser_fixer_timeout=3)
        client._teleport_object.assert_awaited_once_with(123, target, False, 2, False, 3)
        client.body.write_yaw.assert_awaited_once_with(1.2)

    async def test_no_yaw_request_leaves_orientation_unchanged(self):
        client = Client.__new__(Client)
        client.client_object = SimpleNamespace(read_base_address=AsyncMock(return_value=123))
        client.body = SimpleNamespace(write_yaw=AsyncMock())
        client._teleport_object = AsyncMock()
        await client.teleport(XYZ(10, 0, 10))
        client._teleport_object.assert_awaited_once()
        client.body.write_yaw.assert_not_awaited()
