"""Patch failure ordering, lifetime retention and close/install serialization."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from wizwalker.memory import native_safety as native
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import DropsToggleHook


class FakeAPI:
    def __init__(self):
        self.memory = b'ABCDE'
        self.events = []
        self.positions = {1: 0x5000, 2: 0x6000}
        self.fail_write = False
        self.fail_suspend = None

    def threads(self):
        return set(self.positions)

    def suspend(self, tid):
        if self.fail_suspend == tid:
            raise OSError('suspend failed')
        self.events.append(('suspend', tid))
        return tid

    def resume(self, handle):
        self.events.append(('resume', handle))

    def rip(self, handle):
        return self.positions[handle]

    def read(self, address, size):
        self.events.append(('read', self.memory))
        return self.memory

    def write(self, address, value):
        self.events.append(('write', value))
        if self.fail_write:
            self.fail_write = False
            self.memory = value[:2] + self.memory[2:]
            raise OSError('partial write')
        self.memory = value

    @contextmanager
    def writable(self, address, size):
        self.events.append(('protect', address))
        try:
            yield
        finally:
            self.events.append(('protect_restore', address))


class TransactionTests(unittest.TestCase):
    def test_write_and_verification_complete_before_any_resume(self):
        api = FakeAPI()
        native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.memory, b'12345')
        self.assertEqual(api.events[-2:], [('resume', 2), ('resume', 1)])
        self.assertEqual(api.events[:2], [('suspend', 1), ('suspend', 2)])

    def test_busy_pc_does_not_write_and_resumes_every_thread(self):
        api = FakeAPI()
        api.positions[2] = 0x1003
        with self.assertRaises(native.PatchBusy):
            native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.memory, b'ABCDE')
        self.assertFalse(any(event[0] == 'write' for event in api.events))
        self.assertEqual(api.events[-2:], [('resume', 2), ('resume', 1)])

    def test_partial_write_restores_original_before_threads_resume(self):
        api = FakeAPI()
        api.fail_write = True
        with self.assertRaises(OSError):
            native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.memory, b'ABCDE')
        self.assertIn(('write', b'ABCDE'), api.events)
        self.assertEqual(api.events[-2:], [('resume', 2), ('resume', 1)])

    def test_inaccessible_thread_releases_previous_threads_without_writing(self):
        api = FakeAPI()
        api.fail_suspend = 2
        with self.assertRaises(OSError):
            native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.events, [('suspend', 1), ('resume', 1)])

    def test_guard_rechecked_under_suspension_and_foreign_patch_preserved(self):
        api = FakeAPI()
        api.memory = b'OTHER'
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.memory, b'OTHER')
        self.assertFalse(any(event[0] == 'write' for event in api.events))

    def test_repeat_restore_does_not_rewrite(self):
        api = FakeAPI()
        native.patch_once(api, 0x1000, b'12345', b'ABCDE')
        self.assertFalse(any(event[0] == 'write' for event in api.events))

    def test_new_thread_between_enumerations_is_enlisted(self):
        api = FakeAPI()
        snapshots = iter(({1}, {1, 2}, {1, 2}, {1, 2}))
        api.threads = lambda: next(snapshots)
        native.patch_once(api, 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.events[:2], [('suspend', 1), ('suspend', 2)])


class LifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_for_feature_install_and_rejects_new_work(self):
        handler = HookHandler(None, None)
        inside, finish = asyncio.Event(), asyncio.Event()
        obj = SimpleNamespace(unhook=AsyncMock())
        handler._rewrite_autobot = AsyncMock()
        async def install():
            async with handler.feature_mutation():
                inside.set()
                await finish.wait()
                handler._active_hooks[type(obj)] = obj
        task = asyncio.create_task(install())
        await inside.wait()
        close = asyncio.create_task(handler.close())
        await asyncio.sleep(0)
        obj.unhook.assert_not_awaited()
        finish.set()
        await asyncio.gather(task, close)
        obj.unhook.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, 'closing'):
            async with handler.feature_mutation():
                self.fail('closed handler accepted work')

    async def test_retirement_does_not_free_even_after_native_entry_is_restored(self):
        handler = HookHandler(None, None)
        obj = DropsToggleHook(handler)
        obj.disable_drops_bool = 0x9000
        obj._jump_written, obj._installed = True, True
        obj.jump_address, obj.jump_original_bytecode = 0x1000, obj.ORIGINAL
        obj.write_bytes, obj.free = AsyncMock(), AsyncMock()
        await obj.unhook()
        obj.write_bytes.assert_awaited_once_with(0x1000, obj.ORIGINAL)
        obj.free.assert_not_awaited()
        self.assertIn(0x9000, handler._retained_native_allocations)
        self.assertIsNone(obj.disable_drops_bool)

    async def test_failed_entry_restore_retains_export_and_registration_for_retry(self):
        handler = HookHandler(None, None)
        obj = DropsToggleHook(handler)
        handler._active_hooks[DropsToggleHook] = obj
        obj.disable_drops_bool = 0x9000
        obj._jump_written = True
        obj.write_bytes = AsyncMock(side_effect=OSError('patch busy'))
        with self.assertRaises(OSError):
            await handler.deactivate_drops_toggle_hook()
        self.assertIs(handler._active_hooks[DropsToggleHook], obj)
        self.assertEqual(obj.disable_drops_bool, 0x9000)

    async def test_busy_retry_resumes_before_async_wait_and_logs_completion(self):
        api = FakeAPI()
        api.positions[1] = 0x1000
        handler = SimpleNamespace(process=None)
        async def wait(_):
            self.assertEqual(api.events[-2:], [('resume', 2), ('resume', 1)])
            api.positions[1] = 0x5000
        with patch.object(native, 'Win32', return_value=api), patch.object(native.asyncio, 'sleep', side_effect=wait), patch.object(native.diagnostic, 'emit') as emit:
            await native.patch_code(handler, 'test', 0x1000, b'ABCDE', b'12345')
        self.assertEqual(api.memory, b'12345')
        self.assertEqual(emit.call_args.args[2], 'native_patch_complete')


if __name__ == '__main__':
    unittest.main()
