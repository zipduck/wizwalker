"""Persistent adaptation of LaurenzLikeThat's WizWalker PR 37 (58e5eae).

Intermediate hops retain the PR's below-map height. This is a workaround,
not a guarantee of server acceptance or of avoiding combat.
"""
import asyncio
import math

from .errors import ExceptionalTimeout, HookNotActive
from .utils import XYZ, maybe_wait_for_value_with_timeout
from .memory.memory_objects.game_object_template import WizGameObjectTemplate

DEFAULT_DISTANCE = 2000.0
INTERMEDIATE_Z = -131068.0


def checked_distance(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Teleport distance must be finite and nonnegative (0 disables steps)")
    return value


def check_position(value):
    if not all(math.isfinite(v) for v in (value.x, value.y, value.z)):
        raise ValueError("Teleport coordinates must be finite")


async def wait_for_correction(client):
    try:
        await maybe_wait_for_value_with_timeout(
            client.is_server_move_snapbacked, value=True, sleep_time=0.01, timeout=0.3,
        )
    except ExceptionalTimeout:
        return False
    await asyncio.sleep(0.2)
    return True


async def teleport(client, xyz, yaw, options):
    """Called with the client's teleport lock held; no lock recursion per hop."""
    check_position(xyz)
    if yaw is not None and not math.isfinite(yaw):
        raise ValueError("Teleport yaw must be finite")
    # Copy caller-owned coordinates and settings once for the complete request.
    xyz = XYZ(xyz.x, xyz.y, xyz.z)
    step = checked_distance(client._teleport_distance)
    address = await client.client_object.read_base_address()

    async def send(point):
        await client._teleport_object(address, point, **options)

    async def finish():
        if yaw is not None:
            await client.body.write_yaw(yaw)

    # The opt-out is exactly the old path; no mount/pet/observer reads required.
    if step == 0:
        await send(xyz)
        await finish()
        return

    parent = await client.client_object.parent()
    pet_mode = parent is not None and await parent.object_name() == "Player Object"

    async def position():
        if pet_mode:
            value = await client.client_object.location()
        else:
            try:
                value = await client.body.position()
            except HookNotActive:
                # Preserve the earlier ClientHook+MovementHook-only API usage.
                value = await client.client_object.location()
        check_position(value)
        return value

    def remaining(value):
        return math.hypot(xyz.x - value.x, xyz.y - value.y)

    current = await position()
    if remaining(current) > step and not pet_mode:
        equipment = await client.client_object.try_get_equipment_behavior()
        if equipment is not None:
            for item in await equipment.item_list():
                template = await item.object_template()
                if template is None:
                    continue
                template = WizGameObjectTemplate(client.hook_handler, await template.read_base_address())
                name = (await template.object_name()).lower()
                if name.startswith(("mnt", "mount")):
                    step *= 1.4
                    break

    if remaining(current) <= step:
        await send(xyz)
        await finish()
        return

    await client.hook_handler.ensure_server_move_snapback_hook()
    while True:
        distance = remaining(current)
        final_hop = distance <= step
        if final_hop:
            point = xyz
        else:
            # Always re-aim, including when a final-hop correction sends us back.
            point = XYZ(current.x + step * (xyz.x - current.x) / distance,
                        current.y + step * (xyz.y - current.y) / distance,
                        INTERMEDIATE_Z)
        await client.reset_server_move_snapback()
        await send(point)
        corrected = await wait_for_correction(client)
        current = await position()
        if final_hop and not corrected and math.dist(
                (xyz.x, xyz.y, xyz.z), (current.x, current.y, current.z)) <= 10.0:
            await finish()
            return
        # The correction wait paces retries and remains cancellable. No attempt
        # or elapsed-time budget may abandon an intermediate below-map hop.
