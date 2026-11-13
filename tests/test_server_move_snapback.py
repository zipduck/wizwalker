"""Native observer code exercised on local buffers only; no game attachment."""
import asyncio
import ctypes
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from keystone import Ks, KS_ARCH_X86, KS_MODE_64

from wizwalker.client import Client
from wizwalker.memory.handler import HookHandler
from wizwalker.memory.hooks import ServerMoveSnapbackHook as Observer


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def hook(self):
        h=HookHandler(SimpleNamespace(allocate=lambda _:0x9000),None)
        obj=Observer(h,hook_cache={})
        obj.get_jump_address=AsyncMock(return_value=0x1000)
        obj.get_hook_address=AsyncMock(return_value=0x4000)
        obj.read_bytes=AsyncMock(side_effect=lambda addr,size:obj.PREFIX if size==len(obj.PREFIX) else obj.ORIGINAL)
        obj.write_bytes,obj.free=AsyncMock(),AsyncMock()
        return obj

    async def test_context_guard_fails_before_allocating_or_writing(self):
        obj=self.hook()
        obj.read_bytes.side_effect=lambda addr,size:b'X'*size
        with self.assertRaisesRegex(RuntimeError,'context'): await obj.hook()
        obj.get_hook_address.assert_not_awaited()
        obj.write_bytes.assert_not_awaited()

    async def test_export_initialized_before_jump_and_cleanup_is_idempotent(self):
        obj=self.hook()
        await obj.hook()
        self.assertEqual(obj.write_bytes.await_args_list[0].args,(0x9000,b'\x00'))
        self.assertEqual(obj.write_bytes.await_args_list[-1].args[0],0x1000)
        self.assertEqual(len(obj.jump_bytecode),6)
        self.assertLessEqual(len(obj.hook_bytecode),96)
        await obj.unhook(); await obj.unhook()
        obj.free.assert_awaited_once_with(0x9000)
        obj.write_bytes.assert_awaited_with(0x1000,obj.ORIGINAL)

    async def test_partial_patch_failure_restores_entry_before_freeing_export(self):
        obj=self.hook(); events=[]
        async def write(addr,data):
            events.append((addr,data))
            if addr==0x1000 and data.startswith(b'\xe9'): raise OSError('partial')
        obj.write_bytes.side_effect=write
        obj.free.side_effect=lambda addr:events.append(('free',addr))
        with self.assertRaises(OSError): await obj.hook()
        self.assertEqual(events[-2:],[(0x1000,obj.ORIGINAL),('free',0x9000)])
        await obj.unhook()
        self.assertEqual(obj.free.await_count,1)

    async def test_lazy_install_is_serialized_and_failed_unhook_stays_registered(self):
        h=HookHandler(None,None); h._check_for_autobot=AsyncMock()
        async def install(obj):
            obj.server_move_snapback_bool=0x9000
            await asyncio.sleep(0)
        with patch.object(Observer,'hook',side_effect=None) as hooked:
            # Patch the method with a real coroutine to receive self.
            with patch.object(Observer,'hook',install):
                await asyncio.gather(h.ensure_server_move_snapback_hook(),h.ensure_server_move_snapback_hook())
        h._check_for_autobot.assert_awaited_once()
        self.assertEqual(await h.read_server_move_snapback_bool(),0x9000)
        obj=h._active_hooks[Observer]; obj.unhook=AsyncMock(side_effect=OSError('restore'))
        with self.assertRaises(OSError): await h.deactivate_server_move_snapback_hook()
        self.assertIs(h._active_hooks[Observer],obj)
        self.assertEqual(await h.read_server_move_snapback_bool(),0x9000)

    async def test_client_uses_fresh_export_after_rehook(self):
        c=Client.__new__(Client)
        c.hook_handler=SimpleNamespace(read_server_move_snapback_bool=AsyncMock(side_effect=[100,200]),
            read_typed=AsyncMock(return_value=True),write_typed=AsyncMock())
        self.assertTrue(await c.is_server_move_snapbacked())
        await c.reset_server_move_snapback()
        self.assertEqual([a.args[0] for a in c.hook_handler.write_typed.await_args_list],[100,200])

    async def test_native_filter_replay_registers_flags_and_one_byte_store(self):
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.VirtualAlloc.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_uint32]
        api.VirtualAlloc.restype=ctypes.c_void_p
        api.VirtualFree.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32]
        address=api.VirtualAlloc(None,0x24000,0x3000,0x40)
        self.assertTrue(address)
        try:
            flag,node,state,game=address+1024,address+1200,address+1400,address+4096
            obj=Observer(HookHandler(None,None),hook_cache={})
            body=await obj.bytecode_generator([('server_move_snapback_bool',struct.pack('<Q',flag))])
            asm=Ks(KS_ARCH_X86,KS_MODE_64)
            prefix=bytes(asm.asm('push rbx; push rdi; mov rbx,rcx; mov rcx,rdx; mov rax,r8; mov rdi,r9; cmp rax,rax')[0])
            suffix=bytes(asm.asm('mov [rdi],rax; mov [rdi+8],rcx; mov [rdi+16],r8; pushfq; pop rax; mov [rdi+24],rax; pop rdi; pop rbx; ret')[0])
            ctypes.memmove(address,prefix+body+suffix,len(prefix+body+suffix))
            fn=ctypes.WINFUNCTYPE(None,*([ctypes.c_uint64]*4))(address)
            gid=0x123456789abcdef0
            ctypes.c_uint64.from_address(game+0x213a8).value=gid
            ctypes.c_uint64.from_address(node).value=0x12340000
            for candidate,key,wanted in [(gid,0x12,1),(gid^(1<<40),0x12,0),(gid,0x13,0)]:
                ctypes.memmove(flag,b'\x00'+b'\xcc'*7,8)
                ctypes.c_uint64.from_address(node+0x28).value=candidate
                ctypes.c_uint16.from_address(node+0x20).value=key
                original_rax=0xfeed000000000012
                fn(game,node,original_rax,state)
                self.assertEqual(ctypes.string_at(flag,8),bytes([wanted])+b'\xcc'*7)
                rax,rcx,r8,flags=struct.unpack('<4Q',ctypes.string_at(state,32))
                self.assertEqual((rax,rcx,r8),(original_rax,0x12340000,node))
                self.assertEqual(flags&0x41,0x40)
        finally:
            api.VirtualFree(address,0,0x8000)
