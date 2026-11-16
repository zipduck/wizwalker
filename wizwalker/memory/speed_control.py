"""Manual speed writes with a freshly resolved, live native owner.

Never dereference ClientHook's cached export. The only writes occur inside a
thread-frozen transaction, after resolving the game's current-GID registry and
checking the owning control block. No game callbacks or logging while frozen.
"""
from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import struct
import time

from . import feature_diagnostics as diagnostic
from . import native_safety


IMPLEMENTATION = 'speed-owned-transaction-2026-11-16'
MAX_BUCKET_NODES = 64
MAX_RECORDS = 32

# Exact live-build guards are generated from the reviewed native owner lookup,
# control-block destructor and speed reader. Unknown layouts fail closed.
LAYOUT_PROBES = {
    0x16A1BF0: bytes.fromhex(
        '40534883ec20488b1543040c02488bd94885d27415488b92a8130200e8cf1a0200488bc34883c4205bc333c048890148894108488bc34883c4205bc3'),
    0x16C36E0: bytes.fromhex(
        '488bc44889480841564883ec3048c740f0feffffff488958104889701848897820488bda4c8bf133ff8978e848893948897908c740e80100000040383d4f831202740d488d0d4e831202ff15b09a2301488bd348c1ea208bc34833d04823151d831202488bca4803c94c8b05f8821202498b0cc8488d04124c8d14c508000000488b15d18212024d8b0cc00f1f4400004c3bca7505488bc2eb074b8b0410488b00483bc80f84ed000000483959107305488b09ebdb480f47ca483bca0f84d5000000488b59204885db742e8b430885c074118d5001f00fb15308740c8b430885c075ef488bdfeb114885db740c488b7918f0ff4308f0ff430849893e498b760849895e08bfffffffff4885f6742b8bc7f00fc1460883f801751f488b06488bceff50088bc7f00fc1460c83f801750a488b06488bceff5010904885db742b8bc7f00fc1430883f801751f488b03488bcbff50088bc7f00fc1430c83f801750a488b03488bcbff5010904885db74298bc7f00fc1430883f801751d488b03488bcbff5008f00fc17b0c83ff01750a488b03488bcbff501090803df281120200740d488d0df1811202ff152b9b2301498bc6488b5c2448488b742450488b7c24584883c430415ec3cc'),
    0x275EE0: bytes.fromhex(
        '4883ec28488b4910807928007514c6412801ba01000000488b014883c42848ff6008488d1537cc4303488d4c2430e8e18c1602cc'),
    0x20733E0: bytes.fromhex(
        '48895c2410574883ec20e881cf64ffbf02000000488bd88bc7f00fb13db798800174544889742430be010000000f1f0033c0f00fb1359e9880017522488bd3488d0dda92ea00'),
    0x16C4180: bytes.fromhex(
        '40534883ec20488bd9488b4958e84e060000f30f1015eec92c014885c07449f30f10487cf30f10056cb53b010f2fc176030f28ca0fb783800100006685c075070fb783c000000098660f6ec00f5bc0f30f5905f1b02f01f30f58c2f30f59c80f28c14883c4205bc30f28c24883c4205bc3'),
}


class Unavailable(RuntimeError):
    """A normal transition, dead owner or unrecognized native object."""


def thread_ids(buffer, length, pid):
    """Bounded x64 SYSTEM_PROCESS_INFORMATION / SYSTEM_THREAD_INFORMATION.

    Microsoft documents these layouts under NtQuerySystemInformation. Reject
    truncation/layout drift; never silently use a partial set of target threads.
    """
    offset = 0
    while offset + 0x100 <= length:
        next_offset, count = struct.unpack_from('<II', buffer, offset)
        end = offset + next_offset if next_offset else length
        if end > length or end < offset + 0x100:
            raise RuntimeError('Invalid system process snapshot extent')
        if struct.unpack_from('<Q', buffer, offset + 0x50)[0] == pid:
            if not 0 < count <= 4096 or offset + 0x100 + count * 0x50 > end:
                raise RuntimeError('Invalid system thread snapshot count')
            result = set()
            for i in range(count):
                owner, tid = struct.unpack_from('<QQ', buffer, offset + 0x100 + i*0x50 + 0x28)
                if owner != pid or not 0 < tid <= 0xFFFFFFFF or tid in result:
                    raise RuntimeError('Invalid system thread snapshot identity')
                result.add(tid)
            return result
        if not next_offset:
            break
        offset = end
    raise RuntimeError('No live target threads in snapshot')


class SpeedWin32(native_safety.Win32):
    """Fast complete thread snapshots; no Toolhelp walk while game is paused."""
    def __init__(self, process):
        super().__init__(process)
        self._thread_buffer = C.create_string_buffer(4 * 1024 * 1024)
        self._ntdll = C.WinDLL('ntdll')
        self._query = self._ntdll.NtQuerySystemInformation
        self._query.argtypes = [W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        self._query.restype = W.ULONG

    def threads(self):
        length = W.ULONG()
        status = self._query(5, self._thread_buffer, len(self._thread_buffer), C.byref(length))
        if status != 0 or not 0 < length.value <= len(self._thread_buffer):
            raise RuntimeError(f'System thread snapshot failed: {status:#x}')
        return thread_ids(self._thread_buffer, length.value, self.pid)


@dataclass(frozen=True)
class Owner:
    gid: int
    address: int
    control: int
    speed: int

    @property
    def key(self):
        return self.gid, self.address, self.control

    def details(self):
        return dict(gid=hex(self.gid), address=hex(self.address),
                    control=hex(self.control), speed=self.speed)


class LiveLayout:
    """Read-only mirror of the qualified native registry lookup, bounded."""
    def __init__(self, api, base):
        self.api, self.base = api, base

    def q(self, address):
        return struct.unpack('<Q', self.api.read(address, 8))[0]

    def validate(self):
        for rva, expected in LAYOUT_PROBES.items():
            if self.api.read(self.base + rva, len(expected)) != expected:
                raise RuntimeError(f'Speed owner layout is not supported at RVA {rva:#x}')
        for rva, expected in ((0x2F1CFB0, 0x2078B10),
                              (0x298D530, 0x275EE0)):
            if self.q(self.base + rva) != self.base + expected:
                raise RuntimeError('Speed owner vtable does not match the reviewed build')

    def resolve(self, gid=None):
        base = self.base
        if gid is None:
            client = self.q(base + 0x3762040)
            if not client:
                raise Unavailable('no_game_client')
            if self.q(client) != base + 0x2C83548:
                raise Unavailable('wrong_game_client_type')
            gid = self.q(client + 0x213A8)
        if not gid:
            raise Unavailable('no_current_character')
        mask = self.q(base + 0x37EBA60)
        buckets = self.q(base + 0x37EBA48)
        end = self.q(base + 0x37EBA38)
        if not buckets or not end or mask > 0xFFFFF or mask & (mask + 1):
            raise Unavailable('registry_not_ready')
        bucket = buckets + (((gid >> 32) ^ (gid & 0xFFFFFFFF)) & mask) * 16
        node, last = struct.unpack('<QQ', self.api.read(bucket, 16))
        seen = set()
        for _ in range(MAX_BUCKET_NODES):
            if not node or node == end or node in seen:
                break
            seen.add(node)
            next_node, previous, key, address, control = struct.unpack(
                '<QQQQQ', self.api.read(node, 40))
            if key == gid:
                if not address or not control:
                    raise Unavailable('empty_owner')
                vtable, strong, weak, owned = struct.unpack(
                    '<QiiQ', self.api.read(control, 24))
                # The reviewed native destructor takes its object from +0x10.
                # A positive strong count with threads frozen excludes teardown.
                if (vtable != base + 0x298D528 or strong <= 0 or weak <= 0
                        or owned != address):
                    raise Unavailable('expired_or_mismatched_owner')
                data = self.api.read(address, 0xC2)
                if (struct.unpack_from('<Q', data)[0] != base + 0x2F1CFB0
                        or data[0x28] != 0
                        or struct.unpack_from('<Q', data, 0x48)[0] != gid):
                    raise Unavailable('wrong_type_identity_or_destroying')
                return Owner(gid, address, control, struct.unpack_from('<h', data, 0xC0)[0])
            if node == last:
                break
            node = next_node
        raise Unavailable('character_not_in_registry')


def change_once(api, layout, candidate, value, *, restore=False, committed=None):
    """Reacquire ownership and recheck everything after suspension, then write.

    The optimistic candidate is never a write target. Restoration resolves its
    GID through the live registry and also requires the same object/control.
    """
    started = time.perf_counter()
    with native_safety.frozen(api, []):
        owner = layout.resolve(candidate.gid if restore else None)
        if owner.key != candidate.key or owner.speed != candidate.speed:
            raise Unavailable('owner_or_value_changed_before_write')
        original = struct.pack('<h', owner.speed)
        replacement = struct.pack('<h', value)
        address = owner.address + 0xC0
        if original != replacement:
            try:
                api.write(address, replacement)
                if api.read(address, 2) != replacement:
                    raise RuntimeError('Speed write readback failed')
            except BaseException:
                # The validated owner cannot die/recycle before rollback.
                api.write(address, original)
                if api.read(address, 2) != original:
                    raise RuntimeError('CRITICAL: speed rollback verification failed')
                raise
            if committed is not None:
                # Local bookkeeping only; survives a subsequent resume failure.
                committed()
    return (time.perf_counter() - started) * 1000


class SpeedController:
    """Handler-owned; all operations run under its feature/close lock."""
    def __init__(self, handler, *, api=None, layout=None):
        self.handler, self.api, self.layout = handler, api, layout
        self.records = {}  # identity -> (original speed, last requested speed)
        self.blocked = False
        self.last_report = None
        self.last_report_at = 0.0
        self.suppressed = 0

    def emit(self, event, **details):
        diagnostic.emit(self.handler, 'speed', event,
                        speed_implementation=IMPLEMENTATION, **details)

    def report(self, event, reason):
        now = time.monotonic()
        key = event, str(reason)
        if key != self.last_report or now - self.last_report_at >= 30:
            self.emit(event, reason=str(reason), suppressed=self.suppressed)
            self.last_report, self.last_report_at, self.suppressed = key, now, 0
        else:
            self.suppressed += 1

    def prepare(self):
        if self.blocked:
            return False
        if self.layout is None:
            import pymem.process
            try:
                self.api = SpeedWin32(self.handler.process)
                module = pymem.process.module_from_name(
                    self.handler.process.process_handle, 'WizardGraphicalClient.exe')
                if module is None:
                    raise RuntimeError('Game module is unavailable')
                layout = LiveLayout(self.api, module.lpBaseOfDll)
                layout.validate()
                self.layout = layout
                trace = diagnostic.get_trace(self.handler)
                if trace is not None:
                    trace.start()
                self.emit('layout_validated', game_base=hex(module.lpBaseOfDll))
            except Exception as error:
                self.blocked = True
                diagnostic.error(self.handler, 'speed', 'layout_rejected', error)
                return False
        return True

    def apply(self, value):
        if type(value) is not int or not -32768 <= value <= 32767:
            self.report('request_rejected', 'speed must fit a signed 16-bit integer')
            return False
        if not self.prepare():
            return False
        try:
            owner = self.layout.resolve()
            if owner.speed == value:
                # No suspension, writes or repeated logs on the normal hot path.
                if owner.key not in self.records:
                    self.report('write_skipped', 'already_requested_without_saved_original')
                return True
            if (owner.key not in self.records and len(self.records) >= MAX_RECORDS
                    and not any(key[0] == owner.gid for key in self.records)):
                self.report('request_rejected', 'speed restoration record limit reached')
                return False
            self.emit('write_intent', operation='apply', target=owner.details(), value=value)
            previous = self.records.get(owner.key)
            # A game-side speed change becomes the new restoration baseline.
            original = previous[0] if previous and owner.speed == previous[1] else owner.speed
            def committed():
                # Only one registered owner can be resolved for a given GID.
                # Forget retired identities without reading their old addresses.
                for key in tuple(self.records):
                    if key[0] == owner.gid:
                        del self.records[key]
                self.records[owner.key] = original, value
            elapsed = change_once(self.api, self.layout, owner, value, committed=committed)
            self.emit('write_complete', operation='apply', target=owner.details(),
                      value=value, original=original, transaction_ms=elapsed)
            diagnostic.state(self.handler, 'speed', active=True, target=owner.details(),
                             requested=value, tracked=len(self.records))
            self.last_report = None
            return True
        except Unavailable as error:
            self.report('write_skipped', error)
        except Exception as error:
            self.report('write_failed', f'{type(error).__name__}: {error}')
        return False

    def restore(self, reason):
        self.emit('restore_begin', reason=reason, tracked=len(self.records))
        failed = {}
        for key, (original, modified) in list(self.records.items()):
            try:
                owner = self.layout.resolve(key[0])
                if owner.key != key:
                    self.emit('restore_skipped', reason='owner_replaced', gid=hex(key[0]))
                    continue
                if owner.speed != modified:
                    self.emit('restore_skipped', reason='game_changed_speed', target=owner.details())
                    continue
                self.emit('write_intent', operation='restore', reason=reason,
                          target=owner.details(), value=original)
                elapsed = change_once(self.api, self.layout, owner, original, restore=True)
                self.emit('write_complete', operation='restore', target=owner.details(),
                          value=original, transaction_ms=elapsed)
            except Unavailable as error:
                self.emit('restore_skipped', reason=str(error), gid=hex(key[0]))
            except Exception as error:
                failed[key] = original, modified
                diagnostic.error(self.handler, 'speed', 'restore_failed', error)
        self.records = failed
        diagnostic.state(self.handler, 'speed', active=False, pending_restores=len(failed))
        self.emit('restore_complete', reason=reason, failed=len(failed))
