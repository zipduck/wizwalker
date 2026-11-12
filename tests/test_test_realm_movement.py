"""Regression checks for instruction boundaries, branch pairing and camera ABI."""
import re
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wizwalker.client import Client
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import MovementTeleportHook
from wizwalker.memory.memory_objects.game_client import GameClient, CAMERA_TOGGLE_PATTERN


CAMERA_SITE = bytes.fromhex(
    "48 8b 93 e0 22 02 00 48 8b 03 4c 8b 88 88 04 00 00 41 b8 01 00 00 00 "
    "48 8b cb 48 3b fa 75 0e 48 8b 93 d0 22 02 00 41 ff d1 32 c0 eb 05 "
    "41 ff d1 b0 01 88 83 18 23 02 00 48 8b 07 33 d2 48 8b cf ff 50 70 "
    "48 8b 8b 00 23 02 00 48 8b 01 b2 01 ff 50 70"
)


class MovementTests(unittest.IsolatedAsyncioTestCase):
    async def test_trampoline_skips_replay_and_pointer_carry(self):
        handler = HookHandler(None, SimpleNamespace(
            _get_je_instruction_forward_backwards=AsyncMock(return_value=(0x4000, 0x4009))))
        handler.read_bytes = AsyncMock(return_value=bytes.fromhex("0f 84 22 00 00 00 83 fb"))
        hook = MovementTeleportHook(handler, hook_cache={})
        # Deliberately cross a byte boundary; old bytearray[0] += 13 failed here.
        code = await hook.bytecode_generator([("teleport_helper", struct.pack("<Q", 0x12345FFA))])
        self.assertEqual(code[-6:], bytes.fromhex("40 57 48 83 ec 30"))
        replay = len(code) - 6
        for displacement in (0x6E, 0x56):
            at = code.index(b"\xe9" + struct.pack("<i", displacement))
            self.assertEqual(at + 5 + displacement, replay)
        for offset in (8, 12, 13):
            self.assertIn(struct.pack("<Q", 0x12345FFA + offset), code)
        hook.jump_address, hook.hook_address = 0x1000, 0x9000
        jump = await hook.get_jump_bytecode()
        self.assertEqual(len(jump), 6)
        self.assertEqual(jump[-1:], b"\x90")

    async def test_collision_pair_rejected_before_writes(self):
        handler = HookHandler(None, SimpleNamespace(
            _get_je_instruction_forward_backwards=AsyncMock(return_value=(0x4000, 0x4009))))
        hook = MovementTeleportHook(handler, hook_cache={})
        hook.pattern_scan = AsyncMock(side_effect=(0x5000, 0x5056))
        hook.read_bytes = AsyncMock(side_effect=(b"\x74\x7a", b"\x74\x25"))
        hook.write_bytes = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "share the expected exit"):
            await hook.prehook()
        hook.write_bytes.assert_not_awaited()

    async def test_movement_gate_pair_rejects_wrong_target(self):
        client = Client.__new__(Client)
        client._je_instruction_forward_backwards = None
        client.hook_handler = SimpleNamespace(pattern_scan=AsyncMock(return_value=0x1000),
            read_bytes=AsyncMock(side_effect=(b"\x0f\x84" + struct.pack("<i", 50),
                                              b"\x0f\x84" + struct.pack("<i", 50))))
        with self.assertRaisesRegex(RuntimeError, "share an exit"):
            await client._get_je_instruction_forward_backwards()
        self.assertIsNone(client._je_instruction_forward_backwards)

    async def test_camera_offsets_read_from_native_path_and_writers_agree(self):
        self.assertIsNotNone(re.fullmatch(CAMERA_TOGGLE_PATTERN, CAMERA_SITE, re.S))
        game = GameClient(HookHandler(None, None))
        game.pattern_scan = AsyncMock(return_value=0x1000)
        async def read(address, primitive):
            return struct.unpack_from("<I", CAMERA_SITE, address-0x1000)[0]
        game.read_typed = read
        game.read_value_from_offset = AsyncMock(return_value=0)
        game.write_value_to_offset = AsyncMock()
        await game.elastic_camera_controller()
        self.assertEqual(game.read_value_from_offset.call_args.args[0], 0x222D0)
        await game.free_camera_controller()
        self.assertEqual(game.read_value_from_offset.call_args.args[0], 0x222E0)
        await game.selected_camera_controller()
        self.assertEqual(game.read_value_from_offset.call_args.args[0], 0x22300)
        await game.write_selected_camera_controller(0x5000)
        self.assertEqual(game.write_value_to_offset.call_args.args[0], 0x22300)
        await game.is_freecam()
        self.assertEqual(game.read_value_from_offset.call_args.args[0], 0x22318)
        await game.write_is_freecam(True)
        self.assertEqual(game.write_value_to_offset.call_args.args[0], 0x22318)
        self.assertEqual(await game.camera_switch_vtable_offset(), 0x488)

    async def test_camera_stub_has_shadow_space_and_derived_slot(self):
        client = Client.__new__(Client)
        client.game_client = SimpleNamespace(read_base_address=AsyncMock(return_value=0x100000),
            camera_switch_vtable_offset=AsyncMock(return_value=0x4A0))
        client.hook_handler = SimpleNamespace(allocate=AsyncMock(return_value=0x200000),
            write_bytes=AsyncMock(), start_thread=AsyncMock(), free=AsyncMock())
        await client._switch_camera(0x300000, 0x400000)
        code = client.hook_handler.write_bytes.call_args.args[1]
        self.assertEqual(code[:11], bytes.fromhex("50 51 52 41 50 41 51 48 83 ec 20"))
        self.assertIn(bytes.fromhex("48 8b 80 a0 04 00 00"), code)
        self.assertTrue(code.endswith(bytes.fromhex("48 83 c4 20 41 59 41 58 5a 59 58 c3")))
        self.assertEqual(code.count(b"\xff\xd0"), 3)
        # Match native toggle ordering: unregister old input before registering new.
        self.assertLess(code.index(b"\x48\xb8" + struct.pack("<Q", 0x400000)),
                        code.index(b"\x48\xb8" + struct.pack("<Q", 0x300000)))
        client.hook_handler.free.assert_awaited_once_with(0x200000)


if __name__ == "__main__":
    unittest.main()
