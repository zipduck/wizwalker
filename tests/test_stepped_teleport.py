"""PR 37 routing, progress, lifecycle and failure behavior without a game."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from wizwalker import XYZ
from wizwalker.errors import HookNotActive
from wizwalker.client import Client
from wizwalker.memory.hooks import MovementTeleportHook
from wizwalker import stepped_teleport as step


def make_client():
    c = Client.__new__(Client)
    c._teleport_distance = 2000
    c._teleport_lock = asyncio.Lock()
    c._teleport_closing = False
    c._teleport_task = None
    c.state = SimpleNamespace(position=XYZ(0, 0, 10), calls=[])
    c.body = SimpleNamespace(position=AsyncMock(side_effect=lambda: c.state.position), write_yaw=AsyncMock())
    c.client_object = SimpleNamespace(
        read_base_address=AsyncMock(return_value=0x1000), parent=AsyncMock(return_value=None),
        location=AsyncMock(side_effect=lambda: c.state.position),
        try_get_equipment_behavior=AsyncMock(return_value=None))
    c.hook_handler = SimpleNamespace(ensure_server_move_snapback_hook=AsyncMock(), close=AsyncMock())
    c.zone_name = AsyncMock(return_value='Zone/A')
    c.is_loading = AsyncMock(return_value=False)
    c.in_battle = AsyncMock(return_value=False)
    c.reset_server_move_snapback = AsyncMock()
    c.is_server_move_snapbacked = AsyncMock(return_value=False)
    async def raw(address, point, **options):
        c.state.calls.append((address, point, options))
        c.state.position = point
    c._teleport_object = AsyncMock(side_effect=raw)
    return c


class SteppingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.wait = patch.object(step, 'wait_for_correction', AsyncMock(return_value=False))
        self.wait.start()
        self.addCleanup(self.wait.stop)

    async def test_disabled_preserves_old_direct_path_and_keywords(self):
        c = make_client()
        await c.modify_teleport_distance(0)
        await c.teleport(XYZ(9000, 200, 40), yaw=1.2, wait_on_inuse=False,
                         wait_on_inuse_timeout=2, purge_on_after_unuser_fixer=False,
                         purge_on_after_unuser_fixer_timeout=3)
        self.assertEqual(len(c.state.calls), 1)
        self.assertEqual(c.state.calls[0][2], dict(wait_on_inuse=False, wait_on_inuse_timeout=2,
            purge_on_after_unuser_fixer=False, purge_on_after_unuser_fixer_timeout=3))
        c.client_object.parent.assert_not_awaited()
        c.hook_handler.ensure_server_move_snapback_hook.assert_not_awaited()
        c.body.write_yaw.assert_awaited_once_with(1.2)

    async def test_short_and_vertical_only_requests_do_not_install_observer(self):
        for target in (XYZ(10, 0, 10), XYZ(0, 0, 1000), XYZ(2000, 0, 10)):
            c = make_client()
            await c.teleport(target)
            self.assertEqual(len(c.state.calls), 1)
            c.client_object.try_get_equipment_behavior.assert_not_awaited()
            c.hook_handler.ensure_server_move_snapback_hook.assert_not_awaited()

    async def test_long_hops_use_pr_height_then_exact_destination_and_yaw(self):
        c = make_client()
        await c.teleport(XYZ(5000, 0, 50), yaw=1)
        self.assertEqual([(p.x,p.y,p.z) for _,p,_ in c.state.calls],
                         [(2000,0,step.INTERMEDIATE_Z),(4000,0,step.INTERMEDIATE_Z),(5000,0,50)])
        c.hook_handler.ensure_server_move_snapback_hook.assert_awaited_once()
        self.assertEqual(c.reset_server_move_snapback.await_count, 3)
        c.body.write_yaw.assert_awaited_once_with(1)

    async def test_short_minimal_hook_usage_falls_back_to_client_object_position(self):
        c=make_client()
        c.body.position.side_effect=HookNotActive('Player')
        await c.teleport(XYZ(10,0,10))
        c.client_object.location.assert_awaited_once()
        c.hook_handler.ensure_server_move_snapback_hook.assert_not_awaited()

    async def test_mounted_multiplier_and_missing_templates(self):
        c = make_client()
        template = SimpleNamespace(read_base_address=AsyncMock(return_value=123))
        c.client_object.try_get_equipment_behavior.return_value = SimpleNamespace(item_list=AsyncMock(
            return_value=[SimpleNamespace(object_template=AsyncMock(return_value=None)),
                          SimpleNamespace(object_template=AsyncMock(return_value=template))]))
        with patch.object(step, 'WizGameObjectTemplate', return_value=SimpleNamespace(object_name=AsyncMock(return_value='MountDragon'))):
            await c.teleport(XYZ(5000,0,50))
        self.assertEqual([p.x for _,p,_ in c.state.calls], [2800,5000])

    async def test_pet_uses_client_object_position_without_equipment(self):
        c = make_client()
        c.client_object.parent.return_value = SimpleNamespace(object_name=AsyncMock(return_value='Player Object'))
        c.body.position.side_effect = AssertionError('Pet must not read wizard body')
        await c.teleport(XYZ(5000,0,50))
        c.client_object.try_get_equipment_behavior.assert_not_awaited()
        self.assertEqual(len(c.state.calls), 3)

    async def test_reaims_after_a_sideways_server_correction(self):
        c=make_client()
        calls=0
        async def correction(_):
            nonlocal calls
            calls+=1
            if calls==1:
                c.state.position=XYZ(1500,500,10)
                return True
            return False
        with patch.object(step,'wait_for_correction',side_effect=correction):
            await c.teleport(XYZ(5000,0,50))
        second=c.state.calls[1][1]
        self.assertLess(second.y,500)
        self.assertGreater(second.y,0)

    async def test_repeated_no_progress_is_bounded(self):
        c=make_client()
        async def bounce(_):
            c.state.position=XYZ(0,0,10)
            return True
        with patch.object(step,'wait_for_correction',side_effect=bounce):
            with self.assertRaisesRegex(step.TeleportStepError,'no progress'):
                await c.teleport(XYZ(5000,0,50),yaw=1)
        self.assertEqual(len(c.state.calls),step.MAX_STALLED_HOPS)
        c.body.write_yaw.assert_not_awaited()

    async def test_repeated_final_corrections_are_bounded(self):
        c=make_client()
        with patch.object(step,'wait_for_correction',AsyncMock(return_value=True)):
            with self.assertRaisesRegex(step.TeleportStepError,'Final'):
                await c.teleport(XYZ(2500,0,50),yaw=1)
        self.assertEqual(len(c.state.calls),1+step.MAX_FINAL_ATTEMPTS)
        c.body.write_yaw.assert_not_awaited()

    async def test_hop_budget_and_wall_clock_budget(self):
        c=make_client()
        with patch.object(step,'MAX_HOPS',1):
            with self.assertRaisesRegex(step.TeleportStepError,'step limit'):
                await c.teleport(XYZ(5000,0,50))
        c=make_client()
        async def stuck(_): await asyncio.Event().wait()
        with patch.object(step,'MAX_SECONDS',0.01), patch.object(step,'wait_for_correction',side_effect=stuck):
            with self.assertRaisesRegex(step.TeleportStepError,'time limit'):
                await c.teleport(XYZ(5000,0,50))
        self.assertFalse(c._teleport_lock.locked())

    async def test_zone_loading_combat_and_identity_changes_stop_before_next_hop(self):
        for field in ('zone','loading','combat','identity'):
            c=make_client()
            async def change(_):
                if field=='zone': c.zone_name.return_value='Zone/B'
                elif field=='loading': c.is_loading.return_value=True
                elif field=='combat': c.in_battle.return_value=True
                else: c.client_object.read_base_address.return_value=0x2000
                return False
            with patch.object(step,'wait_for_correction',side_effect=change):
                with self.assertRaises(step.TeleportStepError): await c.teleport(XYZ(5000,0,50))
            self.assertEqual(len(c.state.calls),1)

    async def test_invalid_settings_and_coordinates_fail_before_native_writes(self):
        c=make_client()
        for value in (-1,float('inf'),float('nan')):
            with self.assertRaises(ValueError): await c.modify_teleport_distance(value)
        with self.assertRaises(ValueError): await c.teleport(XYZ(float('nan'),0,0))
        self.assertEqual(c.state.calls,[])

    async def test_missing_observer_does_not_send_long_hop(self):
        c=make_client()
        c.hook_handler.ensure_server_move_snapback_hook.side_effect=RuntimeError('missing signature')
        with self.assertRaisesRegex(RuntimeError,'missing signature'): await c.teleport(XYZ(5000,0,50))
        self.assertEqual(c.state.calls,[])

    async def test_concurrent_requests_serialize_and_cancellation_releases_lock(self):
        c=make_client()
        entered, release=asyncio.Event(),asyncio.Event()
        async def wait(_):
            entered.set()
            await release.wait()
            return False
        with patch.object(step,'wait_for_correction',side_effect=wait):
            first=asyncio.create_task(c.teleport(XYZ(5000,0,50)))
            await entered.wait()
            second=asyncio.create_task(c.teleport(XYZ(100,0,10)))
            await asyncio.sleep(0)
            self.assertEqual(len(c.state.calls),1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError): await first
            release.set()
            await second
        self.assertFalse(c._teleport_lock.locked())

    async def test_close_cancels_active_and_queued_teleports_before_unhook(self):
        c=make_client()
        entered=asyncio.Event()
        async def wait(_):
            entered.set()
            await asyncio.Event().wait()
        c.is_running=lambda: True
        c._unpatch_movement_update=AsyncMock()
        with patch.object(step,'wait_for_correction',side_effect=wait):
            first=asyncio.create_task(c.teleport(XYZ(5000,0,50)))
            await entered.wait()
            second=asyncio.create_task(c.teleport(XYZ(100,0,10)))
            await asyncio.sleep(0)
            await c.close()
            with self.assertRaises(asyncio.CancelledError): await first
            with self.assertRaisesRegex(RuntimeError,'closing'): await second
        c.hook_handler.close.assert_awaited_once()

    async def test_raw_request_cancellation_restores_pending_flag_and_both_gates(self):
        c=make_client()
        c._teleport_helper=SimpleNamespace(should_update=AsyncMock(return_value=False),
            write_target_object_address=AsyncMock(),write_position=AsyncMock(),write_should_update=AsyncMock())
        c._get_je_instruction_forward_backwards=AsyncMock(return_value=(100,200))
        c.hook_handler._check_if_hook_active=lambda _:True
        c.hook_handler._get_hook_by_type=lambda _:SimpleNamespace(_old_jes_bytes=(b'first',b'second'))
        writes=[]
        async def write(address,data):
            writes.append((address,data))
            if data==b'\x90'*6: raise asyncio.CancelledError()
        c.hook_handler.write_bytes=AsyncMock(side_effect=write)
        with self.assertRaises(asyncio.CancelledError):
            await Client._teleport_object(c,0x1000,XYZ(1,2,3))
        self.assertEqual(writes[-2:],[(100,b'first'),(200,b'second')])
        c._teleport_helper.write_should_update.assert_awaited_with(False)
