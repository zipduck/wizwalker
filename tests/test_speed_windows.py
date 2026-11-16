"""Native speed transaction against a disposable simulated game, never W101."""
import ctypes as C
from ctypes import wintypes as W
import json
import statistics
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace

from wizwalker.memory.native_safety import Win32
from wizwalker.memory.speed_control import LiveLayout, SpeedController, SpeedWin32

CHILD = r'''
import ctypes as C, json, sys, threading, os, time
k=C.WinDLL('kernel32')
k.VirtualAlloc.argtypes=[C.c_void_p,C.c_size_t,C.c_uint32,C.c_uint32]
k.VirtualAlloc.restype=C.c_void_p
b=k.VirtualAlloc(None,0x3800000,0x3000,4)
if not b: raise RuntimeError('fixture allocation failed')
def q(a,v): C.c_uint64.from_address(a).value=v
client,buckets,node,control,obj=[b+x for x in (0x10000,0x40000,0x41000,0x42000,0x43000)]
gid=0x47000001EB35D9
q(b+0x3762040,client);q(client,b+0x2C83548);q(client+0x213A8,gid)
q(b+0x37EBA60,0);q(b+0x37EBA48,buckets);q(b+0x37EBA38,b+0x45000)
q(buckets,node);q(buckets+8,node)
q(node,b+0x45000);q(node+8,b+0x45000);q(node+16,gid);q(node+24,obj);q(node+32,control)
q(control,b+0x298D528);q(control+8,(2<<32)|1);q(control+16,obj)
q(obj,b+0x2F1CFB0);q(obj+0x48,gid)
strong=C.c_int32.from_address(control+8);speed=C.c_int16.from_address(obj+0xC0)
speed.value=50
stop=threading.Event();errors=[]
def worker():
 while not stop.is_set():
  strong.value=0
  q(obj,b+0x2F203B8)
  speed.value=0x5555
  time.sleep(.001)
  if speed.value!=0x5555:errors.append(speed.value)
  q(obj,b+0x2F1CFB0)
  speed.value=50
  strong.value=1
  time.sleep(.001)
t=threading.Thread(target=worker);t.start()
print(json.dumps({'base':b,'pid':os.getpid()}),flush=True)
sys.stdin.readline();stop.set();t.join()
print(json.dumps({'errors':errors}),flush=True)
'''


@unittest.skipUnless(sys.platform == 'win32' and C.sizeof(C.c_void_p) == 8, 'Windows x64')
class NativeSpeedTests(unittest.TestCase):
    def test_recycling_child_cannot_receive_a_write_while_wrong_type_or_expired(self):
        child = subprocess.Popen([sys.executable, '-u', '-c', CHILD],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        k=C.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.argtypes,k.OpenProcess.restype=[W.DWORD,W.BOOL,W.DWORD],W.HANDLE
        k.CloseHandle.argtypes=[W.HANDLE]
        handle=None
        events=[]
        try:
            metadata=json.loads(child.stdout.readline())
            handle=k.OpenProcess(0x1F0FFF,False,metadata['pid'])
            self.assertTrue(handle)
            process=SimpleNamespace(process_handle=handle)
            api=SpeedWin32(process)
            self.assertEqual(api.threads(), Win32(process).threads())
            trace=SimpleNamespace(emit=lambda feature,event,**kw:events.append((event,kw)),
                                  state=lambda *a,**kw:None,error=lambda *a,**kw:None)
            handler=SimpleNamespace(process=process,client=None,_feature_diagnostics=trace)
            controller=SpeedController(handler,api=api,layout=LiveLayout(api,metadata['base']))
            for _ in range(100):
                controller.apply(400)
                controller.restore('fixture_disable')
                time.sleep(.001)
            child.stdin.write('\n');child.stdin.flush()
            output,errors=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,errors)
            self.assertEqual(json.loads(output)['errors'],[])
            times=[d['transaction_ms'] for e,d in events if e=='write_complete']
            self.assertGreater(len(times),0)
            print('Speed child fixture:',json.dumps(dict(writes=len(times),
                  median_transaction_ms=statistics.median(times),max_transaction_ms=max(times))))
        finally:
            if child.poll() is None:
                try: child.communicate(input='\n',timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill();child.communicate(timeout=10)
            if handle:k.CloseHandle(handle)


if __name__=='__main__':unittest.main()
