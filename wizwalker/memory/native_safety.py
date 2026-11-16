"""Bounded x64 patch transactions and process-lifetime hook allocations.

No awaits, file logging, or game callbacks while target threads are suspended.
Busy instruction pointers are retried after resuming every enlisted thread.
Published code/data are deliberately never recycled in a live process.
"""
import asyncio
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
import os
import struct
from functools import wraps

from . import feature_diagnostics as diagnostic


class PatchBusy(RuntimeError):
    pass


def serialized_feature(function):
    @wraps(function)
    async def call(owner, *args, **kwargs):
        handler = getattr(owner, 'hook_handler', owner)
        async with handler.feature_mutation():
            return await function(owner, *args, **kwargs)
    return call


class THREADENTRY32(C.Structure):
    _fields_ = [(n, W.DWORD) for n in ('dwSize', 'cntUsage', 'th32ThreadID',
                 'th32OwnerProcessID')] + [('tpBasePri', W.LONG), ('tpDeltaPri', W.LONG), ('dwFlags', W.DWORD)]


class Win32:
    def __init__(self, process):
        self.handle = process.process_handle
        self.k = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetProcessId': ([W.HANDLE], W.DWORD),
            'CreateToolhelp32Snapshot': ([W.DWORD, W.DWORD], W.HANDLE),
            'Thread32First': ([W.HANDLE, C.POINTER(THREADENTRY32)], W.BOOL),
            'Thread32Next': ([W.HANDLE, C.POINTER(THREADENTRY32)], W.BOOL),
            'OpenThread': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            'GetProcessIdOfThread': ([W.HANDLE], W.DWORD),
            'SuspendThread': ([W.HANDLE], W.DWORD), 'ResumeThread': ([W.HANDLE], W.DWORD),
            'GetThreadContext': ([W.HANDLE, C.c_void_p], W.BOOL),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'VirtualAllocEx': ([W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, W.DWORD], C.c_void_p),
            'VirtualProtectEx': ([W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            'FlushInstructionCache': ([W.HANDLE, C.c_void_p, C.c_size_t], W.BOOL),
            'ReadProcessMemory': ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
            'WriteProcessMemory': ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        self.pid = self.k.GetProcessId(self.handle)
        if not self.pid or self.pid == os.getpid() or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError('Native patch transactions require a separate x64 process')

    def threads(self):
        snapshot = self.k.CreateToolhelp32Snapshot(4, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            entry = THREADENTRY32()
            entry.dwSize = C.sizeof(entry)
            result = []
            ok = self.k.Thread32First(snapshot, C.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == self.pid:
                    result.append(entry.th32ThreadID)
                ok = self.k.Thread32Next(snapshot, C.byref(entry))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise C.WinError(C.get_last_error())
            if not result:
                raise RuntimeError('No live target threads')
            return set(result)
        finally:
            self.k.CloseHandle(snapshot)

    def suspend(self, tid):
        handle = self.k.OpenThread(0x0002 | 0x0008 | 0x0040, False, tid)
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            if self.k.GetProcessIdOfThread(handle) != self.pid:
                raise RuntimeError('Thread identity changed during patch preparation')
            if self.k.SuspendThread(handle) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            return handle
        except BaseException:
            self.k.CloseHandle(handle)
            raise

    def rip(self, handle):
        # AMD64 CONTEXT is 1232 bytes, 16-byte aligned. CONTROL includes RIP/RSP.
        storage = C.create_string_buffer(1232 + 15)
        pointer = (C.addressof(storage) + 15) & ~15
        C.c_uint32.from_address(pointer + 48).value = 0x100001
        if not self.k.GetThreadContext(handle, pointer):
            raise C.WinError(C.get_last_error())
        return C.c_uint64.from_address(pointer + 248).value

    def resume(self, handle):
        try:
            if self.k.ResumeThread(handle) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
        finally:
            self.k.CloseHandle(handle)

    def read(self, address, size):
        value, count = C.create_string_buffer(size), C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, address, value, size, C.byref(count)) or count.value != size:
            raise C.WinError(C.get_last_error())
        return value.raw

    def write(self, address, value):
        count = C.c_size_t()
        if not self.k.WriteProcessMemory(self.handle, address, value, len(value), C.byref(count)) or count.value != len(value):
            raise C.WinError(C.get_last_error())
        if not self.k.FlushInstructionCache(self.handle, address, len(value)):
            raise C.WinError(C.get_last_error())

    @contextmanager
    def writable(self, address, size):
        old = W.DWORD()
        if not self.k.VirtualProtectEx(self.handle, address, size, 0x40, C.byref(old)):
            raise C.WinError(C.get_last_error())
        try:
            yield
        finally:
            previous = W.DWORD()
            if not self.k.VirtualProtectEx(self.handle, address, size, old.value, C.byref(previous)):
                raise C.WinError(C.get_last_error())


@contextmanager
def frozen(api, ranges):
    enlisted = {}
    try:
        # Reconcile creations during enumeration before touching executable bytes.
        for _ in range(8):
            ids = api.threads()
            for tid in sorted(ids - enlisted.keys()):
                enlisted[tid] = api.suspend(tid)
            if api.threads() <= enlisted.keys():
                break
        else:
            raise PatchBusy('Thread set did not stabilize')
        for handle in enlisted.values() if ranges else ():
            rip = api.rip(handle)
            if any(start <= rip < start + size for start, size in ranges):
                raise PatchBusy(f'Thread executing changed instructions at {rip:#x}')
        yield
    finally:
        failures = []
        for handle in reversed(list(enlisted.values())):
            try:
                api.resume(handle)
            except BaseException as error:
                failures.append(error)
        if failures:
            raise RuntimeError('Could not resume every enlisted thread') from failures[0]


def patch_once(api, address, expected, value):
    if not value or len(expected) != len(value):
        raise ValueError('Patch must replace a nonempty equal-sized instruction span')
    with frozen(api, [(address, len(value))]):
        original = api.read(address, len(value))
        if original == value:
            return  # Failed-install rollback/repeated retirement is idempotent.
        if original != expected:
            raise RuntimeError('Executable bytes changed before patch transaction')
        with api.writable(address, len(value)):
            try:
                api.write(address, value)
                if api.read(address, len(value)) != value:
                    raise RuntimeError('Executable patch readback failed')
            except BaseException:
                # Restore while threads are STILL suspended, including partial writes.
                api.write(address, original)
                if api.read(address, len(original)) != original:
                    raise RuntimeError('CRITICAL: executable rollback verification failed')
                raise


async def patch_code(handler, feature, address, expected, value):
    api = Win32(handler.process)
    diagnostic.emit(handler, feature, 'native_patch_begin', address=hex(address), size=len(value))
    for attempt in range(50):
        try:
            patch_once(api, address, expected, value)
        except PatchBusy as error:
            if attempt == 49:
                diagnostic.error(handler, feature, 'native_patch_busy', error)
                raise
            await asyncio.sleep(0.01)  # All threads resumed before yielding.
        except BaseException as error:
            diagnostic.error(handler, feature, 'native_patch_failed', error)
            raise
        else:
            diagnostic.emit(handler, feature, 'native_patch_complete', address=hex(address), retries=attempt)
            return


def allocate_near(handler, address, size):
    api = Win32(handler.process)
    # Independent code pages survive shared scratch restoration and rehooking.
    origin = address & ~0xFFFF
    for distance in range(0x10000, 0x70000000, 0x10000):
        for candidate in (origin + distance, origin - distance):
            if candidate < 0x10000:
                continue
            result = api.k.VirtualAllocEx(api.handle, candidate, size, 0x3000, 0x40)
            if result:
                retain(handler, 'native_code', result, size)
                return result
    raise RuntimeError('Cannot reserve a private trampoline within rel32 reach')


def retain(handler, feature, address, size):
    records = getattr(handler, '_retained_native_allocations', None)
    if records is None:
        records = handler._retained_native_allocations = {}
    records[address] = size
    diagnostic.emit(handler, feature, 'allocation_retained_until_process_exit',
                    address=hex(address), size=size, allocations=len(records),
                    reserved_bytes=sum((n + 4095) & ~4095 for n in records.values()))


def flush_code(handler, address, size):
    api = Win32(handler.process)
    if not api.k.FlushInstructionCache(api.handle, address, size):
        raise C.WinError(C.get_last_error())


async def retire_exports(hook, feature):
    for name, size in hook.exports:
        address = getattr(hook, name, None)
        if address:
            retain(hook.hook_handler, feature, address, size)
            setattr(hook, name, None)


def register_unwind(hook):
    """Register retained RUNTIME_FUNCTION records before publishing a calling stub."""
    entries = getattr(hook, '_unwind_entries', ())
    if not entries:
        return
    import pymem.process
    api = Win32(hook.process)
    k = api.k
    k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [W.LPCWSTR], W.HMODULE
    local = C.WinDLL('ntdll')
    local_base = k.GetModuleHandleW('ntdll.dll')
    target = C.cast(local.RtlAddFunctionTable, C.c_void_p).value
    remote = pymem.process.module_from_name(api.handle, 'ntdll.dll')
    if remote is None:
        raise RuntimeError('Cannot resolve native unwind registration')
    function = remote.lpBaseOfDll + target - local_base
    if api.read(function, 16) != C.string_at(target, 16):
        raise RuntimeError('Native unwind registration export differs from local image')
    base = hook.hook_address
    offset, table = 0x900, bytearray()
    for start, end, unwind in entries:
        table += struct.pack('<III', start, end, offset)
        api.write(base + offset, unwind)
        offset += (len(unwind) + 3) & ~3
    if len(table) > 0x100 or offset > 0xA00:
        raise RuntimeError('Unwind metadata exceeds private page reservation')
    api.write(base + 0x800, bytes(table))
    # DWORD WINAPI entry(void*): aligned shadow space and BOOL exit status.
    stub = (bytes.fromhex('48 83 ec 28 48 b9') + struct.pack('<Q', base + 0x800)
            + b'\xba' + struct.pack('<I', len(entries)) + bytes.fromhex('49 b8')
            + struct.pack('<Q', base) + bytes.fromhex('48 b8') + struct.pack('<Q', function)
            + bytes.fromhex('ff d0 0f b6 c0 48 83 c4 28 c3'))
    api.write(base + 0xA00, stub)
    k.CreateRemoteThread.argtypes = [W.HANDLE, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p, W.DWORD, C.c_void_p]
    k.CreateRemoteThread.restype = W.HANDLE
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
    k.GetExitCodeThread.argtypes, k.GetExitCodeThread.restype = [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL
    thread = k.CreateRemoteThread(api.handle, None, 0, base + 0xA00, None, 0, None)
    if not thread:
        raise C.WinError(C.get_last_error())
    try:
        if k.WaitForSingleObject(thread, 5000) != 0:
            raise RuntimeError('Native unwind registration did not complete; entry remains untouched')
        result = W.DWORD()
        if not k.GetExitCodeThread(thread, C.byref(result)) or result.value != 1:
            raise RuntimeError('Native unwind registration failed; entry remains untouched')
    finally:
        k.CloseHandle(thread)
    diagnostic.emit(hook.hook_handler, type(hook).__name__, 'unwind_registered',
                    address=hex(base + 0x800), entries=len(entries))
