"""Friend resolver, native argument/trigger ABI and cleanup; no game calls."""
import ctypes
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from keystone import Ks, KS_ARCH_X86, KS_MODE_64
from wizwalker import PatternFailed
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import ChatSendHook


class FriendTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_requires_one_exact_message_and_returns_entry(self):
        obj=ChatSendHook(HookHandler(None,None))
        obj.pattern_scan=AsyncMock(return_value=[0x1000])
        async def read(address,size):
            if size==3:return obj._BUDDY_DISAMBIG_BYTES
            if size==4:return struct.pack('<i',0x4000-address-4)
            return obj._BUDDY_EXPECTED_STRING
        obj.read_bytes=AsyncMock(side_effect=read)
        self.assertEqual(await obj._resolve_buddy_add_func(),0x1000)
        obj.pattern_scan.return_value=[0x1000,0x2000]
        with self.assertRaises(PatternFailed):await obj._resolve_buddy_add_func()
        obj.pattern_scan.return_value=[0x1000]
        obj.read_bytes=AsyncMock(return_value=b'wrong')
        with self.assertRaises(PatternFailed):await obj._resolve_buddy_add_func()

    def mock_hook(self):
        allocations=iter((0x9000,0x9100,0x9200,0x9300))
        obj=ChatSendHook(HookHandler(SimpleNamespace(allocate=lambda size:next(allocations)),None),friend_requests_only=True)
        obj.get_jump_address=AsyncMock(return_value=0x1000)
        obj.get_hook_address=AsyncMock(return_value=0x4000)
        obj.read_bytes=AsyncMock(return_value=obj.ORIGINAL)
        obj._resolve_send_func=AsyncMock(side_effect=AssertionError('chat must not resolve'))
        obj._resolve_buddy_add_func=AsyncMock(return_value=0x7000)
        obj.write_bytes,obj.free=AsyncMock(),AsyncMock()
        return obj

    async def test_guard_initialization_boundaries_and_single_free(self):
        obj=self.mock_hook()
        obj.read_bytes.return_value=bytes(6)
        with self.assertRaisesRegex(RuntimeError,'comparison'):await obj.hook()
        obj.get_hook_address.assert_not_awaited()
        obj.write_bytes.assert_not_awaited()
        obj.read_bytes.return_value=obj.ORIGINAL
        await obj.hook()
        self.assertEqual(len(obj.jump_bytecode),6)
        self.assertEqual(obj.hook_bytecode[-11:-5],obj.ORIGINAL)
        self.assertLessEqual(len(obj.hook_bytecode),300)
        for i,(_,size) in enumerate(obj.exports):
            self.assertEqual(obj.write_bytes.await_args_list[i].args[1],bytes(size))
        obj._resolve_send_func.assert_not_awaited()
        await obj.unhook();await obj.unhook()
        self.assertEqual(obj.free.await_count,4)
        obj.write_bytes.assert_awaited_with(0x1000,obj.ORIGINAL)

    async def test_partial_write_and_resolver_failure_release_exports(self):
        obj=self.mock_hook()
        events=[]
        async def write(address,data):
            events.append((address,data))
            if address==0x1000 and data.startswith(b'\xe9'):raise OSError('partial write')
        obj.write_bytes.side_effect=write
        obj.free.side_effect=lambda address:events.append(('free',address))
        with self.assertRaises(OSError):await obj.hook()
        self.assertEqual(events[-5],(0x1000,obj.ORIGINAL))
        self.assertEqual(obj.free.await_count,4)
        obj=self.mock_hook()
        obj._resolve_buddy_add_func.side_effect=PatternFailed('missing')
        with self.assertRaises(PatternFailed):await obj.hook()
        obj.write_bytes.assert_not_awaited()
        self.assertEqual(obj.free.await_count,4)

    async def test_friend_only_registration_omits_chat_and_preserves_failed_unhook(self):
        h=HookHandler(None,None)
        h._check_for_autobot=AsyncMock()
        async def install(obj):
            for i,(name,_) in enumerate(obj.exports):setattr(obj,name,0x9000+i*0x100)
        with patch.object(ChatSendHook,'hook',install):
            await h.activate_friend_request_hook()
        self.assertNotIn('send_trigger',h._base_addrs)
        self.assertEqual(h._base_addrs['buddy_trigger'],0x9200)
        obj=h._active_hooks[ChatSendHook]
        obj.unhook=AsyncMock(side_effect=OSError('restore failed'))
        with self.assertRaises(OSError):await h.deactivate_chat_send_hook()
        self.assertIs(h._active_hooks[ChatSendHook],obj)
        obj.unhook.side_effect=None
        await h.deactivate_chat_send_hook()
        self.assertEqual(h._base_addrs,{})

    async def test_native_idle_and_one_shot_friend_arguments_and_alignment(self):
        api=ctypes.windll.kernel32
        api.VirtualAlloc.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_uint32]
        api.VirtualAlloc.restype=ctypes.c_void_p
        api.VirtualFree.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32]
        address=api.VirtualAlloc(None,0x24000,0x3000,0x40)
        self.assertTrue(address)
        try:
            callback,state,game=address+512,address+2200,address+4096
            exports=[('send_trigger',address+1024),('send_struct',address+1100),('buddy_trigger',address+1200),('buddy_obj',address+1400)]
            obj=ChatSendHook(HookHandler(None,None),friend_requests_only=True)
            obj._resolve_send_func=AsyncMock(side_effect=AssertionError('chat enabled'))
            obj._resolve_buddy_add_func=AsyncMock(return_value=callback)
            obj.read_bytes=AsyncMock(return_value=obj.ORIGINAL)
            obj.jump_address=0x1000
            body=await obj.bytecode_generator([(n,struct.pack('<Q',a)) for n,a in exports])
            asm=Ks(KS_ARCH_X86,KS_MODE_64)
            prefix=bytes(asm.asm('push rbx; push rdi; sub rsp,8; mov rdi,rcx; xor ebx,ebx')[0])
            suffix=bytes(asm.asm('pushfq; pop rax; add rsp,8; pop rdi; pop rbx; ret')[0])
            code=prefix+body+suffix
            ctypes.memmove(address,code,len(code))
            stub=bytes(asm.asm(f'movabs rax,{state}; inc qword ptr [rax]; mov [rax+8],rcx; mov [rax+16],rdx; mov r10,[rcx+0x18]; mov [rax+24],r10; mov r10,rsp; and r10,15; mov [rax+32],r10; ret')[0])
            ctypes.memmove(callback,stub,len(stub))
            fn=ctypes.WINFUNCTYPE(ctypes.c_uint64,ctypes.c_uint64)(address)
            self.assertEqual(fn(game)&0x40,0x40)
            self.assertEqual(ctypes.c_uint64.from_address(state).value,0)
            gid=0x123456789ABCDEF0
            ctypes.c_uint64.from_address(exports[3][1]+0xE0).value=gid
            ctypes.c_ubyte.from_address(exports[2][1]).value=1
            self.assertEqual(fn(game)&0x40,0x40)
            self.assertEqual(struct.unpack('<5Q',ctypes.string_at(state,40)),(1,exports[3][1],gid,game,8))
            self.assertEqual(ctypes.c_ubyte.from_address(exports[2][1]).value,0)
            fn(game)
            self.assertEqual(ctypes.c_uint64.from_address(state).value,1)
            obj._resolve_send_func.assert_not_awaited()
        finally:api.VirtualFree(address,0,0x8000)


if __name__=='__main__':unittest.main()
