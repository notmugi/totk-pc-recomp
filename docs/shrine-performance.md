# Porting the shrine feedback-layout optimization

This branch ports the layout-retention fix used in the custom Linux 0.4.1 PGO build to the fork at
`afae80c908ab9fc9cdbafa9ed5295c946d80aa21`. It changes the host Vulkan renderer, not game code or shaders.
It contains no game data and no NoGlue, Radar, Autobuild, Master Sword or Material Columns changes.

## Enable it

After building this branch, add the key to the existing `[performance]` section of `totk.ini`:

```ini
[performance]
shrine_barrier = 1
```

Or launch with `TOTK_SHRINE_BARRIER=1`. Restart after changing it. The default is **0**, preserving the fork's
existing behavior until enabled; set it back to 0 for comparisons or rollback. This follows the project's opt-in
policy for renderer changes that have not been validated across GPUs.

Leave `barrier_merge = 0` when measuring this fix alone. That separate experimental feature can keep a pass open
and use a by-region dependency; this fix **still ends the pass and keeps the full memory dependency**. When both
are enabled, the existing merge decision runs first, and layout retention applies only to non-merged barriers.
No new Vulkan feature is required: retention only preserves images already tracked in the feedback layout.

## What was wasting work

In `runtime/vk_exec.c`, `walk` handled ordinary guest `OP_BARRIER` records with `vk_exec_barrier`. That helper
calls `barrier_to` with an empty desired image set, transitioning every tracked feedback image to GENERAL.
The next pass often needs the same attachments in ATTACHMENT_FEEDBACK_LOOP_OPTIMAL_EXT, so `vk_exec_pass_begin`
transitions them straight back. Repeating that in feedback-heavy shrine scenes adds unnecessary layout work.

The fix retains the layouts at this specific barrier while preserving synchronization:

- Source: `ALL_COMMANDS`, `MEMORY_WRITE`.
- Destination: `ALL_COMMANDS`, `MEMORY_READ | MEMORY_WRITE`.
- No by-region flag, no skipped pass end, no removed uniform-buffer flush.

It does not promise an FPS multiplier. Layout counts and timing depend on the scene, GPU, driver and other settings.

## Port the code, not old line numbers

1. Find `barrier_to`, `vk_exec_barrier`, `vk_exec_pass_end`, and `walk` in the destination's `runtime/vk_exec.c`.
   Trace every caller of the full-reset helper. Do not globally replace those calls.
2. Add `guest_barrier` next to `vk_exec_barrier`. End the pass **before** reading `fbl_img`, `fbl_aspect` and `nfbl`:
   pass-end alias/copy handling in another revision may change the tracked set. With the switch enabled and no
   frame dump armed, call `barrier_to(ex, ex->fbl_img, ex->fbl_aspect, ex->nfbl)`. Otherwise call the existing
   full-reset helper. A second pass-end call inside `barrier_to` is harmless because the pass is already closed.
3. Route only the ordinary `OP_BARRIER` fallback through `guest_barrier`. Preserve surrounding statistics and any
   `barrier_merges` decision. Do not turn layout retention into barrier merging.
4. Keep the current `barrier_to` implementation. In this fork it flushes pending uniform updates, resets
   `pass_storage` / `pass_merged`, updates `fbl_serial` when the image set changes, and elides a redundant barrier
   only when no intervening command or layout transition requires one. These behaviors must survive the port.
5. Keep full resets for transfers, texture clears/copies, dispatches, inline work, chunk ends and submission ends.
   In particular, `runtime/vk_contact.c`'s `vk_contact_exec` must call `vk_exec_barrier` before dispatch: its sampled
   and storage images use GENERAL. This fork **already has that fix**; do not add a duplicate barrier.
6. Port the opt-in setting too: `runtime/cfg.c`'s known keys, section and metadata; the `retain_feedback` field in
   `runtime/vk_exec.h`; its initialization in `state_reset`; and the setting documentation. Each replay worker
   calls `state_reset`, so the setting is read outside the hot barrier path. If an older release lacks this config
   API, use its existing startup-setting mechanism rather than copying incompatible config machinery.
7. Adapt and run `tests/shrine_barrier_check.py` against the destination's actual helpers and headers. Do not
   replace the renderer with a duplicate test implementation or edit generated guest C.

## Build and package without losing the PGO baseline

Follow [building.md](building.md) and [pgo.md](pgo.md) for the destination revision. Rebuild the runtime and relink
the executable: copying a changed `.c` file into an installer does not change its precompiled archive or an
already-installed executable.

For a packaged Linux PGO release:

- Start from that exact release's sources, compiler, sysroot, flags, runtime headers and matching per-game PGO
  profile. Do not drop this fork's entire newer renderer into 0.4.1 or substitute an older non-PGO executable.
- Rebuild every runtime object affected by the patch and its headers, for every shipped CPU variant. **This
  branch adds a `VkExec` field and config metadata**, so replacing only `vk_exec.o` is not enough. Rebuild all
  dependent runtime objects; mixing struct layouts across objects is unsafe. An older header-free adaptation
  may have a smaller rebuild scope, but verify dependencies instead of assuming it.
- Preserve the release's bundled SDL, static Opus/libvpx and other link dependencies, generated-code provenance,
  optimization level and matching profiles. This renderer fix requires no generator changes or guest relifting;
  do not reuse generated objects from a different generator/header/patch-set build.
- Where the runtime is a static archive, retain its existing whole-archive link rule:
  `-Wl,--whole-archive libtotkrt.a -Wl,--no-whole-archive`. Omitting it can discard registration-only objects and
  strong overrides while weak defaults satisfy references: a binary can pass `--selftest` yet fail at startup
  with a missing service. Also run `--probe-services` on the newly linked binary.
- Repack the installer with the rebuilt archive(s) and matching sources/headers/config metadata. Check payload
  differences and hashes; do not modify unrelated dependencies. Rebuild/relink installed games separately.
- Before deploying, close the game and back up the existing executable **outside** its folder. Preserve its name,
  launcher path, settings and save/profile location. Do not create a new profile or move the installation.

## Regression check (no game, window or GPU)

Install Clang, Vulkan headers and Vulkan Memory Allocator headers, then run from the repository root:

```sh
python3 tests/shrine_barrier_check.py
python3 tests/settings_doc_check.py
```

If VMA is outside the compiler's include path, pass its directory explicitly:

```sh
python3 tests/shrine_barrier_check.py --include /path/to/include/vma
```

`--include` is repeatable for packaged Vulkan headers too; `CC` selects a compiler command. Use MSYS2 UCRT64 Clang
on Windows, not MSVC. The harness writes only under ignored `build/test/shrine-barrier/` and compiles at `-O0`
and `-O2`. It uses the actual barrier helper source and real `VkExec`/Vulkan headers, with stubbed pass ending,
uniform flushing and Vulkan command recording. It checks:

- Retention for zero through nine images, image/aspect identity, full stage/access dependency and no layout churn.
- Unchanged layout serial when retaining; updated serial and transition counts when resetting/changing targets.
- Pass termination, merge bookkeeping, redundant barrier elision and dependencies after pending UB copies.
- Switch-off behavior, dump fallback and reading the tracked set only after a simulated pass-end alias reset.
- Guest-op routing after the merge decision and the existing contact-compute GENERAL reset.

## Validation status and remaining GPU checks

For this branch: the harness passes at `-O0` and `-O2`; replacing retention with the old full-reset behavior makes
its retention assertion fail. The settings documentation check and Clang syntax checks of `vk_exec.c` and `cfg.c`
pass. These are source-level checks, **not** a new gameplay or performance benchmark.

Not run on this branch: a full game build, `--selftest`, `--probe-services`, `--probe-cfg`, `--probe-kernel`,
`--probe-vkexec`, `--probe-nvn-tex`, `--probe-shader`, GPU validation, screenshots, or FPS/frametime comparisons.
No installed game was modified or launched for this port.

Before promoting the fix, follow [CONTRIBUTING.md](../CONTRIBUTING.md)'s runtime/rendering checks on the rebuilt
binary. Compare whole runs with `TOTK_SHRINE_BARRIER=0` and `1`, keeping `TOTK_BARRIER_MERGE=0`, the same shrine
save, camera/input, resolution, shadow settings and warmed shaders. Run at least two of each; report hardware,
driver, average FPS, lows/worst frames, slow frames and VRAM. Use `TOTK_TRACE=gfx` to compare layout transitions;
use performance tracing separately if tracing overhead affects timing. Test outdoor scenes, contact shadows,
transfers and chunked replay as well. Use `TOTK_VK_VALIDATION=1` and require no new validation errors. Frame dumps
intentionally fall back to the old path and therefore must not be used to measure the optimized path.
