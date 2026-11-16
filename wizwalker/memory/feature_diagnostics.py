"""Local, bounded diagnostics for gardening and Drops; no game-memory reads.

Use an independent file sink because wizwalker disables its Loguru namespace by
default. Events describe Python operations, not every native trampoline call.
"""
import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
import traceback
import warnings

_sink = None
_sink_failed = False


def _write(record):
    global _sink, _sink_failed
    if _sink_failed:
        return
    try:
        if _sink is None:
            path = Path('logs') / f'memory-feature-diagnostics-{os.getpid()}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            _sink = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
            _sink.setFormatter(logging.Formatter('%(message)s'))
        message = json.dumps(record, separators=(',', ':'))
        _sink.handle(logging.LogRecord(__name__, logging.INFO, __file__, 0, message, (), None))
    except Exception as error:
        # Diagnostic I/O must never interrupt hook rollback/restoration.
        _sink_failed = True
        try:
            warnings.warn(f'Memory feature diagnostics unavailable: {error}', RuntimeWarning)
        except Exception:
            pass  # Even warnings-as-errors must not break hook restoration.


class FeatureTrace:
    def __init__(self, handler):
        self.handler = handler
        self.client = getattr(handler, 'client', None)
        process = getattr(handler, 'process', None)
        self.pid = getattr(process, 'process_id', None)
        self.hwnd = getattr(self.client, 'window_handle', None)
        self.sequence = 0
        self.states = {}
        self.recent = deque(maxlen=16)
        self.task = None

    def emit(self, feature, event, **details):
        self.sequence += 1
        record = dict(schema=1, implementation='garden-drops-audit-2026-11-15',
                      time=datetime.now(timezone.utc).isoformat(), monotonic_ns=time.monotonic_ns(),
                      pid=self.pid, hwnd=hex(self.hwnd) if self.hwnd is not None else None,
                      sequence=self.sequence, feature=feature, event=event, **details)
        if event.startswith(('write_', 'free_', 'code_write_', 'flag_write_')):
            self.recent.append(record.copy())
        _write(record)

    def error(self, feature, event, error):
        self.emit(feature, event, error_type=type(error).__name__, error=str(error),
                  traceback=''.join(traceback.format_exception(error))[-4000:],
                  recent=list(self.recent), states=self.states.copy())

    def state(self, feature, **values):
        self.states.setdefault(feature, {}).update(values)
        self.emit(feature, 'state', **self.states[feature])

    def start(self):
        if self.client is not None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._watch())

    async def _watch(self):
        last = time.monotonic()
        try:
            while self.client.is_running():
                await asyncio.sleep(1.0)
                if time.monotonic() - last >= 30:
                    self.emit('session', 'heartbeat', states=self.states.copy(), recent=list(self.recent))
                    last = time.monotonic()
            self.emit('session', 'client_exit', states=self.states.copy(), recent=list(self.recent))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error('session', 'observer_failed', error)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


def get_trace(handler):
    trace = getattr(handler, '_feature_diagnostics', None)
    if trace is None:
        # Offline fixtures without a real client can inject a recording trace;
        # do not create diagnostic files for every bare MemoryHook construction.
        if getattr(handler, 'client', None) is None:
            return None
        trace = FeatureTrace(handler)
        handler._feature_diagnostics = trace
    return trace


def emit(handler, feature, event, **details):
    trace = get_trace(handler)
    if trace is not None:
        trace.emit(feature, event, **details)


def state(handler, feature, **details):
    trace = get_trace(handler)
    if trace is not None:
        trace.state(feature, **details)


def error(handler, feature, event, exception):
    trace = get_trace(handler)
    if trace is not None:
        trace.error(feature, event, exception)


async def write(handler, feature, writer, address, value, *, region):
    details = dict(region=region, address=hex(address), size=len(value),
                   prefix=value[:32].hex(), sha256=hashlib.sha256(value).hexdigest())
    emit(handler, feature, 'write_intent', **details)
    try:
        await writer(address, value)
    except BaseException as exc:
        error(handler, feature, 'write_failed', exc)
        raise
    emit(handler, feature, 'write_complete', **details)


async def free(handler, feature, releaser, address):
    emit(handler, feature, 'free_intent', address=hex(address))
    try:
        await releaser(address)
    except BaseException as exc:
        error(handler, feature, 'free_failed', exc)
        raise
    emit(handler, feature, 'free_complete', address=hex(address))
