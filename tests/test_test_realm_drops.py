"""Drops trampoline and quest return-boundary regressions; no game required."""
import ctypes
import re
import struct
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from wizwalker.client import Client
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import DropsToggleHook, QuestHook


SITE = bytes.fromhex(
    '48 83 ec 68 48 c7 44 24 30 fe ff ff ff 48 8b 05 84 ae 5b 01 '
    '48 33 c4 48 89 44 24 58 48 8d 05 5d 58 85 00 48 89 44 24 20 '
    '48 89 54 24 28 66 0f 6f 05 bb ac 81 00 f3 0f 7f 44 24 48 '
    'c6 44 24 38 00 41 b8 04 00 00 00 48 8d 15 db 24 dd 00 48 8d 4c 24 38')


class DropsTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_uses_current_export_after_reinstallation(self):
        client = Client.__new__(Client)
        client._disable_drops_bool = 0xDEAD
        client.hook_handler = SimpleNamespace(
            _check_if_hook_active=lambda kind: True,
            read_disable_drops_bool=AsyncMock(side_effect=(0x9000, 0xA000)),
            write_bytes=AsyncMock())
        await client.disable_drops()
        await client.enable_drops()
        self.assertEqual([call.args for call in client.hook_handler.write_bytes.await_args_list],
                         [(0x9000,b'\x01'),(0xA000,b'\x00')])

    async def test_unique_callback_context_and_name_guard(self):
        self.assertIsNotNone(re.fullmatch(DropsToggleHook.pattern, SITE, re.S))
        quest_rewards = bytearray(SITE)
        quest_rewards[66] = 12
        self.assertIsNone(re.fullmatch(DropsToggleHook.pattern, quest_rewards, re.S))
        hook = DropsToggleHook(HookHandler(None, None), hook_cache={})
        hook.pattern_scan = AsyncMock(return_value=0x1000)
        hook.read_bytes = AsyncMock(side_effect=(SITE[:13], SITE[70:77], b'Loot\0'))
        self.assertEqual(await hook.get_jump_address(hook.pattern), 0x1000)
        target = 0x1000+77+struct.unpack('<i', SITE[73:77])[0]
        hook.read_bytes.assert_awaited_with(target, 5)
        for name in (b'LootX', b'Fake\0'):
            hook.read_bytes = AsyncMock(side_effect=(SITE[:13], SITE[70:77], name))
            with self.assertRaisesRegex(RuntimeError, 'does not dispatch Loot'):
                await hook.get_jump_address(hook.pattern)

    async def test_replay_and_both_jump_boundaries(self):
        hook = DropsToggleHook(HookHandler(None, None), hook_cache={})
        code = await hook.bytecode_generator([('disable_drops_bool', struct.pack('<Q', 0x9000))])
        self.assertEqual(code[-13:], SITE[:13])
        branch = code.index(b'\x0f\x84')
        self.assertEqual(branch+6+struct.unpack_from('<i', code, branch+2)[0], len(code)-13)
        hook.jump_address, hook.hook_address = 0x1000, 0x5000
        jump = await hook.get_jump_bytecode()
        self.assertEqual(len(jump), 13)
        self.assertEqual(jump[5:], b'\x90'*8)
        self.assertLessEqual(len(code)+5, 50)  # SimpleHook scratch reservation

    async def test_native_flag_branches_preserve_stack(self):
        # Execute generated code only in this test process. The pass-through
        # continuation is a local sentinel, never the game's Loot dispatcher.
        api = ctypes.windll.kernel32
        api.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
        api.VirtualAlloc.restype = ctypes.c_void_p
        api.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
        address = api.VirtualAlloc(None, 4096, 0x3000, 0x40)
        self.assertTrue(address)
        try:
            hook = DropsToggleHook(HookHandler(None, None), hook_cache={})
            code = await hook.bytecode_generator([('disable_drops_bool', struct.pack('<Q', address+512))])
            code += bytes.fromhex('b8 78 56 34 12 48 83 c4 68 c3')
            ctypes.memmove(address, code, len(code))
            flag = ctypes.c_ubyte.from_address(address+512)
            function = ctypes.WINFUNCTYPE(ctypes.c_uint64)(address)
            for value, expected in ((0, 0x12345678), (1, 0), (0, 0x12345678)):
                flag.value = value
                self.assertEqual(function(), expected)
        finally:
            api.VirtualFree(address, 0, 0x8000)

    async def test_quest_returns_after_whole_nine_byte_instruction(self):
        hook = QuestHook(HookHandler(None, None), hook_cache={})
        hook.jump_address, hook.hook_address = 0x2000, 0x6000
        hook.hook_handler.process = type('Process', (), {'allocate': lambda self, size: 0xA000})()
        code = await hook.get_hook_bytecode()
        self.assertEqual(code[-14:-5], QuestHook.ORIGINAL)
        destination = hook.hook_address+len(code)+struct.unpack('<i',code[-4:])[0]
        self.assertEqual(destination, hook.jump_address+9)
        self.assertEqual(len(await hook.get_jump_bytecode()), 9)
        self.assertEqual(QuestHook.exports[0][1], 8)


if __name__ == '__main__':
    unittest.main()
