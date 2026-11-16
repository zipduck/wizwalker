"""Offline diagnostic I/O and write-boundary tests; no live process access."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from loguru import logger
from wizwalker.memory import feature_diagnostics as diagnostic
from wizwalker.memory.hooks import DropsToggleHook
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.memory_reader import MemoryReader


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_intent_precedes_native_call_and_failure_keeps_context(self):
        trace = SimpleNamespace(emit=Mock(), error=Mock())
        handler = SimpleNamespace(_feature_diagnostics=trace)
        async def writer(address, value):
            self.assertEqual(trace.emit.call_args.args, ('NoDrops', 'write_intent'))
            self.assertEqual(trace.emit.call_args.kwargs['address'], '0x9000')
            raise OSError('gone')
        with self.assertRaisesRegex(OSError, 'gone'):
            await diagnostic.write(handler, 'NoDrops', writer, 0x9000, b'\x01', region='flag')
        self.assertEqual(trace.error.call_args.args[:2], ('NoDrops', 'write_failed'))
        self.assertEqual(trace.emit.call_count, 1)  # no false completion

    async def test_drops_actual_wrapper_logs_address_before_write(self):
        handler = HookHandler(None, None)
        events = []
        handler._feature_diagnostics = SimpleNamespace(
            emit=lambda feature, event, **kw: events.append((event, kw)), error=Mock())
        obj = DropsToggleHook(handler)
        obj.disable_drops_bool = 0x9000
        async def write(_self, address, value):
            self.assertEqual(events[-1][0], 'write_intent')
            self.assertEqual(events[-1][1]['region'], 'flag')
            self.assertEqual(address, 0x9000)
            self.assertEqual(value, b'\x01')
        with patch.object(MemoryReader, 'write_bytes', write):
            await obj.write_bytes(0x9000, b'\x01')
        self.assertEqual([event for event, _ in events], ['write_intent', 'write_complete'])

    async def test_observer_records_exit_without_game_memory_reads(self):
        client = SimpleNamespace(window_handle=456, is_running=Mock(side_effect=[True, False]))
        handler = SimpleNamespace(client=client, process=SimpleNamespace(process_id=123))
        trace = diagnostic.FeatureTrace(handler)
        with patch.object(diagnostic, '_write') as write, \
             patch.object(diagnostic.asyncio, 'sleep', AsyncMock()):
            trace.state('NoDrops', installed=True, disabled=False)
            await trace._watch()
        record = write.call_args.args[0]
        self.assertEqual(record['event'], 'client_exit')
        self.assertEqual(record['pid'], 123)
        self.assertEqual(record['states']['NoDrops'], {'installed': True, 'disabled': False})

    async def test_session_close_stops_observer_without_polling_again(self):
        client = SimpleNamespace(window_handle=456, is_running=lambda: True)
        trace = diagnostic.FeatureTrace(SimpleNamespace(client=client, process=None))
        with patch.object(diagnostic, '_write'):
            trace.start()
            await asyncio.sleep(0)
            await trace.stop()
        self.assertIsNone(trace.task)

    async def test_json_file_flush_rotation_and_logging_when_wizwalker_disabled(self):
        previous_sink, previous_failed = diagnostic._sink, diagnostic._sink_failed
        diagnostic._sink, diagnostic._sink_failed = None, False
        logger.disable('wizwalker')
        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(diagnostic, 'Path', return_value=Path(directory)):
                    trace = diagnostic.FeatureTrace(SimpleNamespace(
                        client=SimpleNamespace(window_handle=456), process=SimpleNamespace(process_id=123)))
                    for i in range(20):
                        trace.emit('NoDrops', 'write_complete', address=hex(0x9000+i), size=1)
                sink = diagnostic._sink
                self.assertEqual(sink.maxBytes, 5_000_000)
                self.assertEqual(sink.backupCount, 3)
                records = [json.loads(line) for line in Path(sink.baseFilename).read_text().splitlines()]
                self.assertEqual(len(records), 20)  # file read while still open
                self.assertEqual(records[-1]['pid'], 123)
                self.assertEqual(records[-1]['hwnd'], '0x1c8')
                self.assertEqual(len(trace.recent), 16)
                sink.close()
        finally:
            if diagnostic._sink is not None and diagnostic._sink is not previous_sink:
                diagnostic._sink.close()
            diagnostic._sink, diagnostic._sink_failed = previous_sink, previous_failed

    async def test_log_open_failure_does_not_interrupt_memory_operation(self):
        handler = SimpleNamespace(client=SimpleNamespace(window_handle=456), process=None)
        writer = AsyncMock()
        with patch.object(diagnostic, '_sink', None), patch.object(diagnostic, '_sink_failed', False), \
             patch.object(diagnostic, 'RotatingFileHandler', side_effect=OSError('disk unavailable')), \
             patch.object(diagnostic, 'warnings') as warnings:
            await diagnostic.write(handler, 'NoDrops', writer, 0x9000, b'\x00', region='flag')
        writer.assert_awaited_once_with(0x9000, b'\x00')
        warnings.warn.assert_called_once()


if __name__ == '__main__':
    unittest.main()
