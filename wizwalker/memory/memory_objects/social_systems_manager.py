import struct
from typing import Optional

import regex
import pymem.process

from wizwalker.constants import Primitive
from wizwalker.errors import MemoryReadError, PatternFailed
from wizwalker.memory.memory_object import DynamicMemoryObject, PropertyClass
from wizwalker.memory.handler import HookHandler
from .unlock_trigger_list import DynamicUnlockTriggerList
from .adventure_party_list import DynamicAdventurePartyList


# C++ class: SocialSystemsManager
# Source: WizardGraphicalClient\SocialSystems\SocialSystemsManager.cpp
# Constructor: FUN_141203f30 (r792258)
#
# Class layout (from constructor FUN_141203f30, r792258):
# ==============================================
# Offset  Type                    Description
# ------  ----                    -----------
# 0x000   vtable*                 Main vtable
# 0x048   vtable*                 MessageHandler vtable
# 0x0A8   shared_ptr              -> AdventurePartyList* (MSG_RequestAdventureParty)
# 0x0C8   uint64                  Join requesting player GID (Status 3)
# 0x0E0   uint64                  Join requesting player GID (Status 1)
# 0x0E8   uint64                  Join request party GID (Status 3)
# 0x100   bool                    Group quest flag (MSG_SetGroupQuest)
# 0x108   uint64                  Group Quest GID (MSG_SetGroupQuest)
# 0x110   uint64                  Group Goal GID (MSG_SetGroupQuest)
# 0x128   uint64                  Player GID
# 0x1B0   uint64                  Stored Party GID 1
# 0x1D8   uint64                  Current Party GID
# 0x1E0   UnlockTriggerList       Embedded PropertyClass (0x58 bytes)
# 0x260   uint64                  Group GID
# 0x268   int32                   Adventure party state
# 0x26C   int32                   Adventure party param
# 0x30A   bool                    Guild house flag
# 0x360   float32                 Float (8.0 in guild house)
# 0x364   uint32                  Join cooldown (MSG_RequestAdventureParty)
# 0x378   uint32                  Permission mask (init 0x7F)
# 0x37D   bool                    Zone loaded flag

_EXE_NAME = "WizardGraphicalClient.exe"

# Reviewed test constructor: RSI is the object and R14 is object+0x48.
# Register allocation changed from live's RDI/RSI. This signature is build
# dependent; a matching sequence alone does not validate the field layout.
_SSM_CONSTRUCTOR_PATTERN = (
    rb"\x48\x8D\x05....\x48\x89\x06"
    rb"\x48\x8D\x05....\x49\x89\x06"
    rb"\x48\x8D\x05....\x48\x89\x46\x68"
)
_SSM_NAME_PROBE_OFFSET = 0x537
_SSM_EXPECTED_NAME = b"SocialSystemsManager::MSG_SetFriendlyPlayer\x00"


class SocialSystemsManager(PropertyClass):
    async def read_base_address(self) -> int:
        raise NotImplementedError()

    # -------------------------------------------------------------------------
    # Guild / Party GID fields
    # -------------------------------------------------------------------------

    async def player_gid(self) -> int:
        return await self.read_value_from_offset(0x128, Primitive.uint64)

    async def stored_party_gid(self) -> int:
        """Stored Party GID at 0x1B0.
        Compared in HandleCharacterChanged (FUN_1412223a0, r792258);
        if it differs from the guild's PartyGID, MSG_UNLOCKTRIGGERDATA is re-sent.
        """
        return await self.read_value_from_offset(0x1B0, Primitive.uint64)

    async def current_party_gid(self) -> int:
        """Current Party GID at 0x1D8.
        Written by MSG_UnlockTriggerData and MSG_UpdateGuildUnlockTrigger
        from the "PartyGID" DML field.
        """
        return await self.read_value_from_offset(0x1D8, Primitive.uint64)

    # -------------------------------------------------------------------------
    # UnlockTriggerList (embedded PropertyClass at 0x1E0)
    # -------------------------------------------------------------------------

    async def unlock_trigger_list(self) -> DynamicUnlockTriggerList:
        """Returns the embedded UnlockTriggerList sub-object at offset 0x1E0.

        This PropertyClass is the deserialization target for the "Buffer"
        DML field in MSG_UnlockTriggerData. Its m_unlockTriggerList (at
        sub-object offset 0x48 = SocialSystemsManager offset 0x228) is a
        std::list<shared_ptr<UnlockTriggerInfo>>.
        """
        addr = await self.read_base_address()
        return DynamicUnlockTriggerList(self.hook_handler, addr + 0x1E0)

    # -------------------------------------------------------------------------
    # Zone / guild state flags
    # -------------------------------------------------------------------------

    async def is_guild_house(self) -> bool:
        """Guild house flag at 0x30A.
        True when zone is "GuildHouse" or "GuildGallery".
        """
        return await self.read_value_from_offset(0x30A, Primitive.bool)

    async def zone_loaded_flag(self) -> bool:
        """Zone loaded flag at 0x37D. Cleared on OnZoneLoaded."""
        return await self.read_value_from_offset(0x37D, Primitive.bool)

    async def guild_house_float(self) -> float:
        """Float at 0x360. Set to 8.0 in guild house zones."""
        return await self.read_value_from_offset(0x360, Primitive.float32)

    # -------------------------------------------------------------------------
    # Adventure Party data (MSG_RequestAdventureParty / MSG_RequestJoinAdventureParty)
    # -------------------------------------------------------------------------

    async def adventure_party_list(self) -> Optional[DynamicAdventurePartyList]:
        """Shared pointer to deserialized AdventurePartyList at 0xA8.

        Populated by MSG_RequestAdventureParty (FUN_141208900, r792258) from the
        "Buffer" DML field. Contains the full party hierarchy:
          AdventurePartyList -> [AdventurePartyInfo] -> [AdventurePartyEntryInfo]

        Returns None if no adventure party data has been received yet.
        """
        ptr = await self.read_value_from_offset(0xA8, Primitive.uint64)
        if ptr == 0:
            return None
        return DynamicAdventurePartyList(self.hook_handler, ptr)

    async def join_requesting_player_gid_status3(self) -> int:
        """RequestingPlayerGID at 0xC8.
        Written by MSG_RequestJoinAdventureParty when Status==3
        (join request accepted / you are being invited to a party).
        """
        return await self.read_value_from_offset(0xC8, Primitive.uint64)

    async def join_requesting_player_gid_status1(self) -> int:
        """RequestingPlayerGID at 0xE0.
        Written by MSG_RequestJoinAdventureParty when Status==1
        (someone is asking to join YOUR party).
        """
        return await self.read_value_from_offset(0xE0, Primitive.uint64)

    async def join_request_party_gid(self) -> int:
        """PartyGID at 0xE8.
        Written by MSG_RequestJoinAdventureParty when Status==3
        (the party GID for the join invitation).
        """
        return await self.read_value_from_offset(0xE8, Primitive.uint64)

    async def join_cooldown(self) -> int:
        """Join cooldown at 0x364 (uint32).
        Written by MSG_RequestAdventureParty from the "JoinCooldown" DML field.
        """
        return await self.read_value_from_offset(0x364, Primitive.uint32)

    # -------------------------------------------------------------------------
    # Adventure Party / Group fields
    # -------------------------------------------------------------------------

    async def adventure_party_state(self) -> int:
        """State flag at 0x268. Non-zero triggers party creation in OnZoneLoaded."""
        return await self.read_value_from_offset(0x268, Primitive.int32)

    async def adventure_party_param(self) -> int:
        return await self.read_value_from_offset(0x26C, Primitive.int32)

    async def group_gid(self) -> int:
        return await self.read_value_from_offset(0x260, Primitive.uint64)

    # -------------------------------------------------------------------------
    # Group Quest fields (MSG_SetGroupQuest)
    # -------------------------------------------------------------------------

    async def has_group_quest(self) -> bool:
        """Group quest flag at 0x100. Set by MSG_SetGroupQuest handler."""
        return await self.read_value_from_offset(0x100, Primitive.bool)

    async def group_quest_gid(self) -> int:
        """Group Quest GID at 0x108 (uint64).
        Written by MSG_SetGroupQuest from the "QuestGID" DML field.
        """
        return await self.read_value_from_offset(0x108, Primitive.uint64)

    async def group_goal_gid(self) -> int:
        """Group Goal GID at 0x110 (uint64).
        Written by MSG_SetGroupQuest from the "GoalGID" DML field.
        """
        return await self.read_value_from_offset(0x110, Primitive.uint64)

    # -------------------------------------------------------------------------
    # Misc fields
    # -------------------------------------------------------------------------

    async def default_permission_mask(self) -> int:
        """Permission mask at 0x378. Init 0x7F (7-bit bitmask)."""
        return await self.read_value_from_offset(0x378, Primitive.uint32)


class DynamicSocialSystemsManager(DynamicMemoryObject, SocialSystemsManager):
    pass


class CurrentSocialSystemsManager(SocialSystemsManager):
    """Read-only constructor/vtable discovery of the current social manager.

    The historical singleton getter cache was not populated in the observed
    clients. Discover the heap object using all three constructor vtables,
    reject ambiguity, and revalidate a cached object before each use. No
    fallback to a first match or an address inside the executable is allowed.
    This validates object identity, not every party/guild field or payload.
    """

    def __init__(self, hook_handler: HookHandler):
        super().__init__(hook_handler)
        self._ssm_addr = None
        self._ssm_vtables = None
        self._ssm_module_range = None

    async def read_base_address(self) -> int:
        if self._ssm_addr is not None:
            if await self._is_ssm_candidate(self._ssm_addr):
                return self._ssm_addr
            self._ssm_addr = None
        self._ssm_addr = await self._find_ssm_via_constructor_pattern()
        return self._ssm_addr

    async def _is_ssm_candidate(self, address: int) -> bool:
        start, end = self._ssm_module_range
        if not address or address % 8 or start <= address < end:
            return False
        try:
            # One bounded read includes all three vptrs, including the third
            # base at +0x68 that the previous locator never checked.
            data = await self.hook_handler.read_bytes(address, 0x70)
        except MemoryReadError:
            return False
        return all(struct.unpack_from("<Q", data, offset)[0] == expected
                   for offset, expected in zip((0, 0x48, 0x68), self._ssm_vtables))

    async def _find_ssm_via_constructor_pattern(self) -> int:
        module = pymem.process.module_from_name(
            self.hook_handler.process.process_handle, _EXE_NAME
        )
        if module is None:
            raise RuntimeError(f"{_EXE_NAME} module not found")
        start, end = module.lpBaseOfDll, module.lpBaseOfDll + module.SizeOfImage
        match = await self.hook_handler.pattern_scan(
            _SSM_CONSTRUCTOR_PATTERN, module=_EXE_NAME
        )
        probe = match + _SSM_NAME_PROBE_OFFSET
        if not start <= match < probe + 7 <= end:
            raise RuntimeError("SocialSystemsManager constructor outside module")
        instruction = await self.hook_handler.read_bytes(probe, 7)
        if instruction[:3] != b"\x48\x8d\x15":
            raise RuntimeError("SocialSystemsManager constructor name probe changed")
        name = probe + 7 + struct.unpack_from("<i", instruction, 3)[0]
        if not start <= name <= end - len(_SSM_EXPECTED_NAME):
            raise RuntimeError("SocialSystemsManager constructor name outside module")
        if await self.hook_handler.read_bytes(name, len(_SSM_EXPECTED_NAME)) != _SSM_EXPECTED_NAME:
            raise RuntimeError("SocialSystemsManager constructor name mismatch")

        sequence = await self.hook_handler.read_bytes(match, 31)
        vtables = tuple(match + offset + 7 + struct.unpack_from("<i", sequence, offset + 3)[0]
                        for offset in (0, 10, 20))
        if len(set(vtables)) != 3 or any(v % 8 or not start <= v <= end - 8 for v in vtables):
            raise RuntimeError("SocialSystemsManager constructor vtables invalid")
        self._ssm_vtables = vtables
        self._ssm_module_range = (start, end)
        try:
            matches = await self.hook_handler.pattern_scan(
                regex.escape(struct.pack("<Q", vtables[0])), return_multiple=True
            )
        except PatternFailed:
            matches = []
        if isinstance(matches, int):
            matches = [matches]
        candidates = []
        for address in set(matches):
            if await self._is_ssm_candidate(address):
                candidates.append(address)
        if len(candidates) != 1:
            raise RuntimeError(
                f"Expected one SocialSystemsManager object; found {len(candidates)} "
                "validated candidates. The client may be loading or the layout has changed."
            )
        return candidates[0]
