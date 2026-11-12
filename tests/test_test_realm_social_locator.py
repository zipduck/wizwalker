"""Social locator identity, ambiguity and cache tests; no running game required."""
import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import regex
from wizwalker.errors import MemoryReadError, PatternFailed, PatternMultipleResults
from wizwalker.memory.memory_objects import social_systems_manager as s


class SocialLocatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = 0x140000000
        self.match = self.base + 0x11C248A
        self.vtables = tuple(self.base + x for x in (0x2C3F818, 0x2C3F888, 0x2C3F890))
        self.sequence = bytes.fromhex('48 8d 05 87 d3 a7 01 48 89 06 48 8d 05 ed d3 a7 01 49 89 06 48 8d 05 eb d3 a7 01 48 89 46 68')
        self.address = 0x200000000
        self.data = bytearray(0x70)
        for offset, vtable in zip((0, 0x48, 0x68), self.vtables):
            struct.pack_into('<Q', self.data, offset, vtable)
        self.memory = {
            self.match: self.sequence,
            self.match + 0x537: bytes.fromhex('48 8d 15 28 9d a7 01'),
            self.base + 0x2C3C6F0: s._SSM_EXPECTED_NAME,
            self.address: self.data,
        }
        async def read(address, size):
            if address not in self.memory:
                raise MemoryReadError(address)
            return bytes(self.memory[address][:size])
        async def scan(pattern, **kwargs):
            if 'module' in kwargs:
                return self.match
            self.assertEqual(pattern, regex.escape(struct.pack('<Q', self.vtables[0])))
            return list(self.hits)
        self.hits = [self.address]
        self.handler = SimpleNamespace(process=SimpleNamespace(process_handle=1),
                                       pattern_scan=AsyncMock(side_effect=scan), read_bytes=AsyncMock(side_effect=read))
        self.obj = s.CurrentSocialSystemsManager(self.handler)
        module = SimpleNamespace(lpBaseOfDll=self.base, SizeOfImage=0x4000000)
        patcher = patch.object(s.pymem.process, 'module_from_name', return_value=module)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_captured_constructor_and_cached_identity(self):
        self.assertIsNotNone(regex.fullmatch(s._SSM_CONSTRUCTOR_PATTERN, self.sequence, regex.DOTALL))
        self.assertEqual(await self.obj.read_base_address(), self.address)
        self.assertEqual(self.obj._ssm_vtables, self.vtables)
        self.assertEqual(await self.obj.read_base_address(), self.address)
        self.assertEqual(self.handler.pattern_scan.await_count, 2)
        self.handler.read_bytes.assert_awaited_with(self.address, 0x70)

    async def test_single_hit_still_requires_all_three_vtables(self):
        for offset in (0, 0x48, 0x68):
            saved = bytes(self.data)
            struct.pack_into('<Q', self.data, offset, self.base + 0x1000)
            with self.assertRaisesRegex(RuntimeError, 'found 0'):
                await self.obj.read_base_address()
            self.assertIsNone(self.obj._ssm_addr)
            self.data[:] = saved

    async def test_rejects_ambiguity_and_filters_unreadable_module_unaligned_hits(self):
        self.hits = [self.base + 0x1000, self.address + 1, self.address + 0x1000, self.address]
        self.assertEqual(await self.obj.read_base_address(), self.address)
        self.obj._ssm_addr = None
        self.memory[self.address + 0x1000] = bytes(self.data)
        with self.assertRaisesRegex(RuntimeError, 'found 2'):
            await self.obj.read_base_address()
        self.assertIsNone(self.obj._ssm_addr)

    async def test_stale_cache_is_invalidated_and_rediscovered(self):
        self.assertEqual(await self.obj.read_base_address(), self.address)
        new_address = self.address + 0x1000
        self.memory[new_address] = bytes(self.data)
        del self.memory[self.address]
        self.hits = [self.address, new_address]
        self.assertEqual(await self.obj.read_base_address(), new_address)
        del self.memory[new_address]
        with self.assertRaisesRegex(RuntimeError, 'found 0'):
            await self.obj.read_base_address()
        self.assertIsNone(self.obj._ssm_addr)

    async def test_constructor_guards_and_no_scan_fallback(self):
        for bad in (b'wrong', s._SSM_EXPECTED_NAME[:-1] + b'X'):
            self.memory[self.base + 0x2C3C6F0] = bad
            with self.assertRaisesRegex(RuntimeError, 'name mismatch'):
                await self.obj.read_base_address()
        self.memory[self.base + 0x2C3C6F0] = s._SSM_EXPECTED_NAME
        sequence = bytearray(self.sequence)
        struct.pack_into('<i', sequence, 3, -0x2000000)
        self.memory[self.match] = sequence
        with self.assertRaisesRegex(RuntimeError, 'vtables invalid'):
            await self.obj.read_base_address()
        self.memory[self.match] = self.sequence
        self.handler.pattern_scan.side_effect = PatternMultipleResults('ambiguous constructor')
        with self.assertRaises(PatternMultipleResults):
            await self.obj.read_base_address()
        self.handler.pattern_scan.side_effect = [self.match, PatternFailed('no heap hits')]
        with self.assertRaisesRegex(RuntimeError, 'found 0'):
            await self.obj.read_base_address()

    async def test_cancellation_is_not_swallowed_as_a_bad_candidate(self):
        await self.obj.read_base_address()
        self.handler.read_bytes.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.obj.read_base_address()


if __name__ == '__main__':
    unittest.main()
