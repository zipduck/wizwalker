# November 12, 2026 live port

Base: `development` at `06079a1ea746e29431507f03ea21255d41751f6d`. Selectively adopt the guarded custom test implementation at `872bb2b4f073c04f2a95f69b714307eaadf43b07`, retaining the shutdown race fix, plus maximum-distance-target accessors from the later test source. Test branches are preserved.

Reviewed live PE SHA-256: `d410a2463e7ff0e221d6d11ca10fa53de265abd17cacfba211f9eeca8ec8511a`. All 68 changed combat-property mappings were independently recaptured from the current named-property registration; see `live-2026-11-12-combat-layout-evidence.json`. The older test fixture remains historical and is separately tested. The current subcircle lookup compares +0x164, with a verified ResolveCombatRound action-node +0x58 call chain.

Startup, eight normal hooks, independent pointer identities, drops flag cycle, 10-unit teleport and return, freecam switch/independent position and short camera-destination teleport passed on PID 48572. Original position and all monitored code bytes restored. Native camera switching resolves +0x498 from instructions, avoiding stale hardcoded slots.

[PR 30's November 12 pattern revert](https://github.com/Deimos-Wizard101/wizwalker/commit/ec2ab674395bdd04cff4345a0b451fbf2472a9ef) supports the older test-style scratch/root/movement/drops code generation. It is not a reason to import the whole PR or assert a specific compiler version. The new zone-counter feature and unrelated Steam/login edits were not adopted. Our custom guards, complete scratch restoration, friend-only activation and comprehensive combat mapping are retained.

No live combat, outbound social/chat, fishing, deck, long-distance teleport or comprehensive UI validation is claimed. Known inherited scanner gaps remain. No server-correction workaround is included.

Detailed evidence and source hashes: `C:/Users/coven/OneDrive/Documents/ChatGPT/w101/live_update_2026_11_12/`. Deimos's matching `docs/live-update-2026-11-12.md` and the canonical shared inventory contain integration scope, limitations, final revisions and test totals.
