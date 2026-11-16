"""Ownership races and restoration without a game process."""
import asyncio
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from wizwalker.memory.handler import HookHandler
from wizwalker.memory.speed_control import (
    LiveLayout, SpeedController, Unavailable, LAYOUT_PROBES, change_once, thread_ids,
)


class API:
    def __init__(self):
        self.memory = {}
        self.writes = []
        self.suspended = set()
        self.events = []
        self.on_suspend = None
        self.fail_suspend = False
        self.fail_write = False
        self.bad_readback = False

    def put(self, address, data):
        self.memory.update({address+i: value for i, value in enumerate(data)})

    def q(self, address, value):
        self.put(address, struct.pack('<Q', value))

    def read(self, address, size):
        return bytes(self.memory[address+i] for i in range(size))

    def write(self, address, value):
        assert self.suspended == {1, 2}, 'write before all threads suspended'
        self.events.append('write')
        self.writes.append((address, value))
        if self.fail_write:
            self.fail_write = False
            self.put(address, value[:1])
            raise OSError('partial write')
        if self.bad_readback:
            self.bad_readback = False
            self.put(address, b'\x7f'*len(value))
        else:
            self.put(address, value)

    def threads(self):
        return {1, 2}

    def suspend(self, tid):
        if tid == 2 and self.fail_suspend:
            raise OSError('cannot suspend')
        self.suspended.add(tid)
        if tid == 2 and self.on_suspend:
            callback, self.on_suspend = self.on_suspend, None
            callback()
        return tid

    def resume(self, handle):
        self.events.append('resume')
        self.suspended.remove(handle)

    def rip(self, handle):
        raise AssertionError('data transactions do not need instruction contexts')


def fixture():
    api = API()
    base, client, buckets, node, control, obj = 0x140000000, 0x10000, 0x40000, 0x41000, 0x42000, 0x43000
    gid = 0x47000001EB35D9
    for rva, expected in LAYOUT_PROBES.items():
        api.put(base+rva, expected)
    api.q(base+0x2F1CFB0, base+0x2078B10)
    api.q(base+0x298D530, base+0x275EE0)
    api.q(base+0x3762040, client)
    api.q(client, base+0x2C83548)
    api.q(client+0x213A8, gid)
    api.q(base+0x37EBA60, 0)
    api.q(base+0x37EBA48, buckets)
    api.q(base+0x37EBA38, 0x45000)
    api.put(buckets, struct.pack('<QQ', node, node))
    api.put(node, struct.pack('<QQQQQ', 0x45000, 0x45000, gid, obj, control))
    api.put(control, struct.pack('<QiiQ', base+0x298D528, 1, 2, obj))
    api.put(obj, bytes(0xC2))
    api.q(obj, base+0x2F1CFB0)
    api.q(obj+0x48, gid)
    api.put(obj+0xC0, struct.pack('<h', 50))
    layout = LiveLayout(api, base)
    handler = HookHandler(None, None)
    events = []
    handler._feature_diagnostics = SimpleNamespace(
        emit=lambda feature, event, **kw: events.append((event, kw, set(api.suspended))),
        state=Mock(), error=Mock(), start=Mock(), stop=AsyncMock(), states={})
    controller = SpeedController(handler, api=api, layout=layout)
    handler._speed_controller = controller
    return SimpleNamespace(**locals())


class OwnerTests(unittest.TestCase):
    def test_layout_guards_reject_code_and_vtable_drift(self):
        f = fixture()
        f.layout.validate()
        for address in (f.base+next(iter(LAYOUT_PROBES)), f.base+0x2F1CFB0):
            original = f.api.read(address, 1)
            f.api.put(address, bytes([original[0]^1]))
            with self.assertRaises(RuntimeError):
                f.layout.validate()
            f.api.put(address, original)

    def test_live_owner_only_and_bounded_registry(self):
        for mutation in ('null_client', 'bad_client', 'no_gid', 'no_owner', 'strong_zero',
                         'negative_count', 'wrong_control', 'wrong_pair', 'wrong_type',
                         'wrong_gid', 'destroying', 'cycle', 'bad_mask', 'missing_memory'):
            with self.subTest(mutation=mutation):
                f = fixture()
                if mutation == 'null_client': f.api.q(f.base+0x3762040, 0)
                if mutation == 'bad_client': f.api.q(f.client, 123)
                if mutation == 'no_gid': f.api.q(f.client+0x213A8, 0)
                if mutation == 'no_owner': f.api.q(f.node+32, 0)
                if mutation == 'strong_zero': f.api.put(f.control+8, bytes(4))
                if mutation == 'negative_count': f.api.put(f.control+8, b'\xff'*4)
                if mutation == 'wrong_control': f.api.q(f.control, f.base+0x1234)
                if mutation == 'wrong_pair': f.api.q(f.control+16, f.obj+8)
                if mutation == 'wrong_type': f.api.q(f.obj, f.base+0x2F203B8)
                if mutation == 'wrong_gid': f.api.q(f.obj+0x48, 7)
                if mutation == 'destroying': f.api.put(f.obj+0x28, b'\x01')
                if mutation == 'cycle':
                    f.api.q(f.node+16, 7); f.api.q(f.node, f.node)
                    f.api.q(f.buckets+8, f.node+200)
                if mutation == 'bad_mask': f.api.q(f.base+0x37EBA60, 10)
                if mutation == 'missing_memory': del f.api.memory[f.control]
                self.assertFalse(f.controller.apply(400))
                self.assertEqual(f.api.writes, [])

    def test_unchanged_polls_do_not_suspend_or_write(self):
        f = fixture()
        self.assertTrue(f.controller.apply(400))
        f.api.suspend = Mock(side_effect=AssertionError('unexpected suspension'))
        for _ in range(100): self.assertTrue(f.controller.apply(400))
        self.assertEqual(len(f.api.writes), 1)
        self.assertEqual(f.controller.records[f.layout.resolve().key], (50, 400))

    def test_race_between_preview_and_frozen_validation_never_writes(self):
        for mutation in ('expired', 'reused', 'selection', 'value'):
            f = fixture()
            def mutate():
                if mutation == 'expired': f.api.put(f.control+8, bytes(4))
                if mutation == 'reused': f.api.q(f.obj, f.base+0x2F203B8)
                if mutation == 'selection': f.api.q(f.client+0x213A8, 0)
                if mutation == 'value': f.api.put(f.obj+0xC0, struct.pack('<h', 77))
            f.api.on_suspend = mutate
            self.assertFalse(f.controller.apply(400))
            self.assertEqual(f.api.writes, [])
            self.assertFalse(f.api.suspended)
            self.assertFalse(f.controller.records)

    def test_suspend_failure_and_partial_write_or_bad_readback_fail_closed(self):
        for mode in ('fail_suspend', 'fail_write', 'bad_readback'):
            f = fixture(); setattr(f.api, mode, True)
            self.assertFalse(f.controller.apply(400))
            self.assertEqual(f.layout.resolve().speed, 50)
            self.assertFalse(f.api.suspended)
            self.assertFalse(f.controller.records)
            if mode != 'fail_suspend':
                self.assertEqual(f.api.events, ['write', 'write', 'resume', 'resume'])

    def test_restore_current_owned_original_and_no_logging_while_frozen(self):
        f = fixture()
        self.assertTrue(f.controller.apply(400))
        f.controller.restore('disable')
        self.assertEqual(f.layout.resolve().speed, 50)
        self.assertEqual(f.controller.records, {})
        self.assertEqual([v for a, v in f.api.writes], [struct.pack('<h',400), struct.pack('<h',50)])
        self.assertTrue(all(not suspended for _, _, suspended in f.events))
        events = [event for event, _, _ in f.events]
        self.assertLess(events.index('write_intent'), events.index('write_complete'))

    def test_restore_selection_does_not_need_cached_current_pointer(self):
        f = fixture(); f.controller.apply(400)
        f.api.q(f.client+0x213A8, 0)
        # Still-owned original object can be restored via its registry GID.
        f.controller.restore('disable_at_selection')
        self.assertEqual(f.layout.resolve(f.gid).speed, 50)

    def test_restore_does_not_write_dead_replaced_or_game_changed_objects(self):
        for mutation in ('expired', 'wrong_type', 'new_character', 'changed_speed', 'new_control'):
            f = fixture(); f.controller.apply(400); f.api.writes.clear()
            if mutation == 'expired': f.api.put(f.control+8, bytes(4))
            if mutation == 'wrong_type': f.api.q(f.obj, f.base+0x2F203B8)
            if mutation == 'new_character': f.api.q(f.node+16, f.gid+1)
            if mutation == 'changed_speed': f.api.put(f.obj+0xC0, struct.pack('<h', 90))
            if mutation == 'new_control':
                f.api.put(f.control+100, f.api.read(f.control, 24)); f.api.q(f.node+32, f.control+100)
            f.controller.restore('unhook')
            self.assertFalse(f.api.writes)
            self.assertFalse(f.controller.records)

    def test_restore_revalidates_under_suspension(self):
        f = fixture(); f.controller.apply(400); f.api.writes.clear()
        f.api.on_suspend = lambda: f.api.put(f.control+8, bytes(4))
        f.controller.restore('shutdown')
        self.assertFalse(f.api.writes)
        self.assertFalse(f.api.suspended)

    def test_restore_failure_keeps_retry_record(self):
        f = fixture(); f.controller.apply(400)
        f.api.fail_write = True
        f.controller.restore('disable')
        self.assertEqual(f.layout.resolve().speed, 400)
        self.assertEqual(len(f.controller.records), 1)
        f.controller.restore('unhook')
        self.assertEqual(f.layout.resolve().speed, 50)

    def test_setting_changes_preserve_baseline_but_game_changes_replace_it(self):
        f = fixture(); f.controller.apply(400); f.controller.apply(200)
        self.assertEqual(next(iter(f.controller.records.values())), (50, 200))
        f.api.put(f.obj+0xC0, struct.pack('<h', 65))
        f.controller.apply(300); f.controller.restore('disable')
        self.assertEqual(f.layout.resolve().speed, 65)

    def test_invalid_values_and_repeated_errors_are_bounded(self):
        f = fixture()
        for value in (32768, -32769, True, 5.0):
            self.assertFalse(f.controller.apply(value))
        f.api.put(f.control+8, bytes(4))
        for _ in range(100): f.controller.apply(400)
        self.assertFalse(f.api.writes)
        self.assertEqual(sum(event == 'write_skipped' for event, _, _ in f.events), 1)

    def test_thread_snapshot_parser_rejects_partial_or_inconsistent_sets(self):
        good = bytearray(0x1A0)
        struct.pack_into('<I', good, 4, 2)
        struct.pack_into('<Q', good, 0x50, 123)
        struct.pack_into('<QQ', good, 0x128, 123, 1)
        struct.pack_into('<QQ', good, 0x178, 123, 2)
        self.assertEqual(thread_ids(good, len(good), 123), {1, 2})
        for mode in ('truncated', 'wrong_owner', 'duplicate', 'no_threads', 'oversize', 'extent', 'missing'):
            data, length, pid = bytearray(good), len(good), 123
            if mode == 'truncated': length -= 1
            if mode == 'wrong_owner': struct.pack_into('<Q', data, 0x178, 124)
            if mode == 'duplicate': struct.pack_into('<Q', data, 0x180, 1)
            if mode == 'no_threads': struct.pack_into('<I', data, 4, 0)
            if mode == 'oversize': struct.pack_into('<I', data, 4, 4097)
            if mode == 'extent': struct.pack_into('<I', data, 0, length+1)
            if mode == 'missing': pid = 999
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                thread_ids(data, length, pid)

    def test_same_character_replacement_is_captured_fresh_without_old_pointer_read(self):
        f = fixture(); f.controller.apply(400)
        replacement = f.obj+0x1000
        f.api.put(replacement, f.api.read(f.obj,0xC2))
        f.api.put(replacement+0xC0,struct.pack('<h',75))
        f.api.q(f.node+24,replacement); f.api.q(f.control+16,replacement)
        for address in range(f.obj,f.obj+0xC2): del f.api.memory[address]
        self.assertTrue(f.controller.apply(400))
        self.assertEqual(len(f.controller.records),1)
        f.controller.restore('disable')
        self.assertEqual(f.layout.resolve().speed,75)

    def test_write_record_survives_resume_failure(self):
        f = fixture(); original_resume = f.api.resume
        def fail_resume(handle):
            original_resume(handle)
            if handle==1: raise RuntimeError('simulated resume failure')
        f.api.resume=fail_resume
        self.assertFalse(f.controller.apply(400))
        self.assertEqual(next(iter(f.controller.records.values())), (50,400))
        f.api.resume=original_resume
        f.controller.restore('disable')
        self.assertEqual(f.layout.resolve().speed,50)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_close_restores_before_hooks_and_rejects_late_poll(self):
        f = fixture(); await f.handler.set_speed_multiplier(400)
        async def close_hooks():
            self.assertEqual(f.layout.resolve().speed, 50)
        f.handler._close_hooks = AsyncMock(side_effect=close_hooks)
        await f.handler.close()
        self.assertFalse(await f.handler.set_speed_multiplier(400))
        self.assertEqual(f.layout.resolve().speed, 50)

    async def test_disable_serializes_with_pending_writer(self):
        f = fixture()
        await f.handler._feature_lock.acquire()
        setter = asyncio.create_task(f.handler.set_speed_multiplier(400))
        await asyncio.sleep(0)
        restore = asyncio.create_task(f.handler.restore_speed_multiplier())
        f.handler._feature_lock.release()
        await asyncio.gather(setter, restore)
        self.assertEqual(f.layout.resolve().speed, 50)

    async def test_cancelled_waiting_writer_cannot_apply_after_restore(self):
        f = fixture()
        await f.handler._feature_lock.acquire()
        setter = asyncio.create_task(f.handler.set_speed_multiplier(400))
        await asyncio.sleep(0); setter.cancel()
        await asyncio.gather(setter, return_exceptions=True)
        f.handler._feature_lock.release()
        await f.handler.restore_speed_multiplier()
        self.assertFalse(f.api.writes)

    async def test_shutdown_and_unhook_permanently_reject_late_poll(self):
        for reason in ('shutdown','unhook'):
            f=fixture(); await f.handler.set_speed_multiplier(400)
            await f.handler.restore_speed_multiplier(reason)
            self.assertFalse(await f.handler.set_speed_multiplier(400))
            self.assertEqual(f.layout.resolve().speed,50)

    async def test_client_close_restores_even_if_other_cleanup_fails(self):
        from wizwalker.client import Client
        f=fixture(); await f.handler.set_speed_multiplier(400)
        client=SimpleNamespace(is_running=lambda:True,hook_handler=f.handler,
                               _unpatch_movement_update=AsyncMock(side_effect=RuntimeError('other cleanup')))
        with self.assertRaisesRegex(RuntimeError,'other cleanup'):
            await Client.close(client)
        self.assertEqual(f.layout.resolve().speed,50)
        self.assertFalse(await f.handler.set_speed_multiplier(400))


if __name__ == '__main__':
    unittest.main()
