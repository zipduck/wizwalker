"""Exercise Win32 transactions against a disposable child, never Wizard101."""
import asyncio
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace

from wizwalker.memory import native_safety as native


CHILD = r'''
import ctypes as C, json, sys, threading, os
k=C.WinDLL('kernel32')
k.VirtualAlloc.argtypes=[C.c_void_p,C.c_size_t,C.c_uint32,C.c_uint32]
k.VirtualAlloc.restype=C.c_void_p
p=k.VirtualAlloc(None,4096,0x3000,0x40)
C.memmove(p,bytes.fromhex('b8 01 00 00 00 c3'),6)
print(json.dumps({'address':p, 'pid':os.getpid()}),flush=True)
f=C.WINFUNCTYPE(C.c_uint64)(p)
stop=threading.Event()
errors=[]
def worker():
 while not stop.is_set():
  result=f()
  if result not in (1,2): errors.append(result); break
threads=[threading.Thread(target=worker) for _ in range(2)]
for t in threads:t.start()
sys.stdin.readline()
stop.set()
for t in threads:t.join()
print(json.dumps({'errors':errors}),flush=True)
'''


@unittest.skipUnless(sys.platform == 'win32' and C.sizeof(C.c_void_p) == 8, 'Windows x64 fixture')
class NativeWindowsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_thread_context_patch_near_allocation_and_unwind_registration(self):
        child = subprocess.Popen([sys.executable, '-u', '-c', CHILD], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        k=C.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes,k.OpenProcess.restype=[W.DWORD,W.BOOL,W.DWORD],W.HANDLE
        k.CloseHandle.argtypes=[W.HANDLE]
        handle=None
        try:
            metadata=json.loads(child.stdout.readline())
            address=metadata['address']
            # Windows venv redirectors can have a different PID from the runtime.
            handle=k.OpenProcess(0x1F0FFF, False, metadata['pid'])
            self.assertTrue(handle)
            process=SimpleNamespace(process_handle=handle)
            handler=SimpleNamespace(process=process,client=None)
            api=native.Win32(process)
            self.assertEqual(api.read(address,6),bytes.fromhex('b8 01 00 00 00 c3'))
            self.assertGreaterEqual(len(api.threads()),3)
            for expected,value in ((1,2),(2,1)):
                await native.patch_code(handler,'fixture',address,
                    b'\xb8'+expected.to_bytes(4,'little'), b'\xb8'+value.to_bytes(4,'little'))
            private=native.allocate_near(handler,address,4096)
            self.assertLess(abs(private-address), 0x70000000)
            # Register a correctly described frame without executing game code.
            api.write(private,bytes.fromhex('48 81 ec d8 00 00 00 48 81 c4 d8 00 00 00 c3'))
            hook=SimpleNamespace(process=process,hook_handler=handler,hook_address=private,
                _unwind_entries=[(0,15,bytes.fromhex('01 07 02 00 07 01 1b 00'))])
            native.register_unwind(hook)
            child.stdin.write('\n'); child.stdin.flush()
            output, errors = child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,errors)
            self.assertEqual(json.loads(output)['errors'],[])
        except BaseException:
            if child.poll() is not None:
                print('Fixture exited:', child.returncode, child.stderr.read())
            raise
        finally:
            if child.poll() is None:
                try:
                    child.communicate(input='\n',timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=10)
            if handle:
                k.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
