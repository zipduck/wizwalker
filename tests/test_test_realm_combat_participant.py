"""Participant sigil layout from the reviewed native lookup; no game writes."""
import struct
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from wizwalker.constants import Primitive
from wizwalker.memory.memory_objects.combat_participant import DynamicCombatParticipant


class ParticipantLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_accessors_match_captured_named_property_instructions(self):
        fixture = json.loads((Path(__file__).parents[1] / 'docs/test-realm-combat-layout-evidence.json').read_text())
        # The fixture contains captured native LEAs paired with property names,
        # not a second copy of source-code offsets. Exercise readers and writers.
        for method, name in fixture['methods'].items():
            with self.subTest(method=method, property=name):
                native = fixture['properties'][name]['test']
                instruction = bytes.fromhex(native['instruction_bytes'])
                self.assertIn(instruction[:3], (bytes.fromhex('44 8d 8b'), bytes.fromhex('4c 8d 87')))
                offset = struct.unpack('<I', instruction[3:])[0]
                self.assertEqual(offset, int(native['offset'], 16))
                p = DynamicCombatParticipant.__new__(DynamicCombatParticipant)
                readers = ['read_value_from_offset', 'read_string_from_offset', 'read_linked_list', 'read_shared_linked_list', 'read_enum']
                writers = ['write_value_to_offset', 'write_string_to_offset', 'write_enum']
                for reader in readers:
                    setattr(p, reader, AsyncMock(return_value=[] if 'list' in reader else 0))
                for writer in writers:
                    setattr(p, writer, AsyncMock())
                p.fetch_npc_behavior_template = AsyncMock(return_value=None)
                await getattr(p, method)(*([1] if method.startswith('write_') else []))
                calls = [getattr(p, io).await_args for io in readers + writers if getattr(p, io).await_count]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[0], offset)

    async def test_reader_and_writer_follow_native_subcircle_comparison(self):
        # Test RVA 0x1FD64B0, reached from ResolveCombatRound with the action's
        # node+0x58 in R8D -> R15D. This is not the similar team-id lookup.
        comparison = bytes.fromhex('44 39 b8 64 01 00 00')
        offset = struct.unpack('<I',comparison[3:])[0]
        participant = DynamicCombatParticipant.__new__(DynamicCombatParticipant)
        participant.read_value_from_offset = AsyncMock(return_value=4)
        participant.write_value_to_offset = AsyncMock()
        self.assertEqual(await participant.subcircle(),4)
        participant.read_value_from_offset.assert_awaited_once_with(offset,Primitive.int32)
        await participant.write_subcircle(0)
        participant.write_value_to_offset.assert_awaited_once_with(offset,0,Primitive.int32)


if __name__ == '__main__':
    unittest.main()
