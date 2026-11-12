"""Memory-backed tests: no game process or installed executable required."""
import asyncio
import re
import struct
import unittest
from unittest.mock import AsyncMock, patch

from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import ClientHook, RootWindowHook


class ScratchTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_region_restored_after_hook_use(self):
        handler = HookHandler(None, None)
        original = bytes((i * 17 + 3) % 256 for i in range(handler.AUTOBOT_SIZE))
        memory = bytearray(original + b"NEIGHBOR")

        async def read(address, size):
            self.assertEqual(address, 0x1000)
            return bytes(memory[:size])

        async def write(address, data):
            self.assertEqual(address, 0x1000)
            self.assertLessEqual(len(data), handler.AUTOBOT_SIZE)
            memory[:len(data)] = data

        handler.pattern_scan = AsyncMock(return_value=0x1000)
        handler.read_bytes, handler.write_bytes = read, write
        await handler._check_for_autobot()
        await handler._check_for_autobot()
        handler.pattern_scan.assert_awaited_once()
        self.assertEqual(memory[:handler.AUTOBOT_SIZE], bytes(handler.AUTOBOT_SIZE))
        # A later hook can use the end of the region; restoring only the
        # original regex-sized prefix must not leave this byte overwritten.
        memory[handler.AUTOBOT_SIZE - 1] = 0xCC
        with patch("wizwalker.memory.handler.asyncio.sleep", new=AsyncMock()):
            await handler.close()
        self.assertEqual(memory, original + b"NEIGHBOR")
        self.assertIsNone(handler._autobot_address)
        self.assertEqual(handler._autobot_pos, 0)

    async def test_reservation_cannot_exceed_capacity_or_move_backwards(self):
        handler = HookHandler(None, None)
        handler._autobot_address = 0x1000
        self.assertEqual(await handler._get_open_autobot_address(handler.AUTOBOT_SIZE), 0x1000)
        with self.assertRaises(RuntimeError):
            await handler._get_open_autobot_address(1)
        for size in (0, -1):
            with self.assertRaises(ValueError):
                await handler._get_open_autobot_address(size)
        self.assertEqual(handler._autobot_pos, handler.AUTOBOT_SIZE)

    async def test_no_write_when_reading_original_region_fails(self):
        handler = HookHandler(None, None)
        handler.pattern_scan = AsyncMock(return_value=0x1000)
        handler.read_bytes = AsyncMock(side_effect=OSError("unreadable"))
        handler.write_bytes = AsyncMock()
        with self.assertRaises(OSError):
            await handler._prepare_autobot()
        handler.write_bytes.assert_not_awaited()


class ClientHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_window_uses_r15_for_capture_and_replay(self):
        site = bytes.fromhex("49 8b 8f d8 00 00 00 48 8b 01 49 8b 97 c8 00 00 00 ff 50 70 84")
        self.assertIsNotNone(re.fullmatch(RootWindowHook.pattern, site, re.DOTALL))
        old_register = bytearray(site)
        old_register[2] = 0x8D  # R13: old wildcard signature incorrectly allowed this.
        self.assertIsNone(re.fullmatch(RootWindowHook.pattern, old_register, re.DOTALL))
        hook = RootWindowHook(HookHandler(None, None), hook_cache={})
        code = await hook.bytecode_generator([("current_root_window_addr", struct.pack("<Q", 0x12345000))])
        self.assertEqual(code[1:8], bytes.fromhex("49 8b 87 d8 00 00 00"))
        self.assertEqual(code[-8:], b"\x58" + site[:7])
        self.assertEqual(hook.instruction_length, 7)

    async def test_updated_site_and_exact_instruction_replay(self):
        # Captured test-realm code: byte 18 ends mov rbx,[rax], followed by
        # the complete seven-byte instruction and the changed rsp+40 load.
        site = bytes.fromhex(
            "18 48 8b 9b c0 01 00 00 48 8b 7c 24 40 48 85 ff 74 29 "
            "8b c6 f0 0f c1 47 08 83 f8 01 75 1d 48 8b 07 48 8b cf "
            "ff 50 08 f0 0f c1 77 0c"
        )
        self.assertIsNotNone(re.fullmatch(ClientHook.pattern, site, re.DOTALL))
        old_site = bytearray(site)
        old_site[12] = 0x38
        self.assertIsNone(re.fullmatch(ClientHook.pattern, old_site, re.DOTALL))
        wrong_replay = bytearray(site)
        wrong_replay[4] = 0xC8
        self.assertIsNone(re.fullmatch(ClientHook.pattern, wrong_replay, re.DOTALL))
        hook = ClientHook(HookHandler(None, None), hook_cache={})
        hook.pattern_scan = AsyncMock(return_value=0x5000)
        self.assertEqual(await hook.get_jump_address(hook.pattern), 0x5001)
        code = await hook.bytecode_generator([("current_client_addr", struct.pack("<Q", 0x12345000))])
        self.assertEqual(code[:4], bytes.fromhex("50 48 8b c7"))  # save RAX; capture RDI
        self.assertEqual(code[-8:], b"\x58" + site[1:8])  # restore RAX and replay
        self.assertEqual(hook.instruction_length, len(site[1:8]))
        hook.jump_address, hook.hook_address = 0x5001, 0x10000
        jump = await hook.get_jump_bytecode()
        self.assertEqual(len(jump), hook.instruction_length)
        self.assertEqual(hook.jump_address + 5 + struct.unpack("<i", jump[1:5])[0], hook.hook_address)
        self.assertEqual(jump[5:], b"\x90\x90")


if __name__ == "__main__":
    unittest.main()
