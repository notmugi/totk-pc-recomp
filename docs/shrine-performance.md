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

## Important: layout retention is not the whole shrine optimization

If shrines are still slow with `shrine_barrier = 1`, this fork also includes upstream **barrier merging**. It targets
another cost: ending and reopening render passes at the game's texture-invalidating barriers. Retaining layouts
alone does not remove those pass breaks. On a build that contains both features, the combined opt-in setup is:

```ini
[performance]
shrine_barrier = 1
barrier_merge = 1
```

Add the keys to the existing section, not a second copy of it. Restart the game. No rebuild is needed to enable
`barrier_merge` in a release that already includes it. The existing feedback-layout mode is required for merging.
These switches do not lower resolution, shadow quality or other graphics settings. To isolate a visual problem,
set only `barrier_merge = 0` and restart, leaving layout retention enabled.

### What the history actually says

- [`61a407b`](https://github.com/notmugi/totk-pc-recomp/commit/61a407b0e1f0bcd26caa31dc93eceba618e60379)
  introduced `barrier_merge`. Its accompanying documentation explicitly names **Nachoyah Shrine's water** and
  reports roughly **1,200 to 130 passes**, **10,000 to 150 layout transitions**, and **24.5 to 7.8 ms GPU time** in
  the upstream test. These are upstream measurements, not measurements of this branch or a promised gain.
- [`af13294`](https://github.com/notmugi/totk-pc-recomp/commit/af1329440d171ada7b0ded1627327138a90f3754)
  guarded storage/vertex reads and 3D BC stand-ins, removed the per-draw storage scan while merging is disabled,
  and documented the cross-pixel ordering caveat below. Both commits are present in the tested 0.5.1-rc1 base.
- `barrier_merge` was **off by default at introduction**, and remains off in the inspected history through
  `3c5ae128308ead14f8a3678aedfafdef7a91b6a7`. There is no default-on-to-off change for that key in that history.
  The older custom 0.3.13/0.4.1 **layout-retention patch was unconditional**; it had no `shrine_barrier` switch.
  That is a different optimization. This history cannot establish settings used by every private build or launcher.

This establishes a documented shrine bottleneck and an existing optimization, **not the commit responsible for
any particular user's regression**. Do not describe enabling merging as proof that a regression was bisected.

### Possible visual artifacts, and the evidence behind the warning

The original commit described a picture that looked the same to the eye but differed at edges by about one pixel
in screenshot comparisons, with the difference not fully explained. The later source comment explains the risk:
NVN's `ORDER_FRAGMENTS` orders accesses across pixels, whereas the in-pass Vulkan **by-region** dependency does not
provide the same cross-region guarantee. A shader sampling a neighbouring region can therefore read a value whose
write was not ordered by that dependency. The comment cites RADV as running the barrier for the whole target;
that observation is not a portability guarantee for every driver or future release.

Examples of what to watch for (possible symptoms, **not reproduced captures from this test**):

- Water/reflection edges briefly flickering or showing square/tile-shaped discontinuities.
- Decal edges, such as markings over a surface, showing a thin incorrect fringe or flickering patch.
- Particle edges briefly looking broken or blocky where they overlap a rendered surface.

No verified example images are included here. Do not label unrelated water/DLAA glitches as evidence of this
particular issue. A symptom should disappear with only `barrier_merge` disabled before attributing it to merging.
It is entirely possible to see no artifacts on a particular GPU/driver and scene.

### Local user test, November 23, 2026

The fix was adapted to the installed **0.5.1-rc1 base `3c5ae12`**, preserving its PGO toolchain/profile and current
guest-code patches. The system used an **RX 7900 XTX with RADV, Mesa 26.3.1**. With layout retention enabled but
merging disabled, the user reported about **75 FPS in a shrine**. After enabling `barrier_merge = 1`, the user
reported **"everything looks fine."** No post-enable FPS figure or controlled A/B benchmark was supplied, so this
records a positive visual report, not a measured performance recovery or all-scenes validation. The shrine name
was not established; do not assume this was the upstream Nachoyah test.

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

## Updating a Linux PGO AppImage

An AppImage update and an installed-game update are separate operations. Replacing the installer alone does not
replace an existing `totk` executable. Keep the same installer filename/path if the installation's rebuild record
points there; do not rename the game folder or change its profile path.

### 1. Back up and extract the exact release

Close the installer before replacement. Work under an ignored `build/` directory, outside the game installation.
Set `IMAGE` to the existing installer and use a fresh work directory for each release:

```sh
IMAGE=/absolute/path/to/TotK-Setup-0.5.1-rc1-x86_64-pgo.AppImage
WORK="$PWD/build/appimage-shrine-update"
mkdir -p "$WORK/package" "$WORK/work"
export TMPDIR="$WORK/work"
cp -p "$IMAGE" "$WORK/original.AppImage"
sha256sum "$WORK/original.AppImage"
(cd "$WORK/package" && "$WORK/original.AppImage" --appimage-extract)
PKG="$WORK/package/squashfs-root"
cat "$PKG/VERSION"
```

Record a file-hash and symlink-target manifest of the untouched payload. Obtain the source revision named by
`VERSION`, then apply/adapt the shrine patch to that source, not to whichever `main` happens to exist today.
For 0.5.1-rc1 the base was `3c5ae12`; preserve its null-attachment helpers when resolving the insertion beside
`vk_exec_barrier`. Keep the source and test changes described earlier in this guide.

### 2. Rebuild both runtime variants with the matching package toolchain

Set `SRC` to the patched source checkout, `GEN` to the matching local generated-code directory, and `EXEFS`,
`ROMFS`, `PROFILE` to the existing installation's paths. These are build inputs/default paths, not instructions
to copy, move or edit saves. This release's builder requires `GEN/layout.json` even for a runtime-only build.
Use the release's packaged builder so its PGO/code-layout flags stay intact:

```sh
for CPU in v3 v2; do
    python3 "$PKG/tools/build_game.py" \
        --version v143 --cpu "$CPU" --jobs 8 \
        --make-runtime-lib --runtime "$SRC/runtime" \
        --gen "$GEN" --out "$WORK/rt-$CPU" \
        --toolchain "$PKG/llvm" --sysroot "$PKG/llvm/sysroot" \
        --deps "$PKG/runtime/deps" --pgo "$PKG/data/pgo-v143.profdata" \
        --exefs "$EXEFS" --romfs "$ROMFS" --profile "$PROFILE" || exit 1
done
```

**Packaging trap:** the shipped sysroot may have headers pruned because players normally compile only guest code
and install-side sources. A full runtime rebuild can therefore fail on missing SDL/Opus/VPX headers. Restore the
missing headers from the exact matching full release SDK in a separate build-toolchain copy; check its library
versions/hashes against the package first. Do not substitute arbitrary host headers or ship the expanded SDK by
accident. In that case, point the build flags above at the expanded build copy while keeping `PKG` as the clean
shipping payload.

Decide defaults **before** building. The public branch keeps both switches opt-in. The user's custom AppImage
sets `BARRIER_MERGE` and `SHRINE_BARRIER` metadata defaults in `runtime/cfg.c` to `1`, with matching comments in
`totk.ini.example`. This makes both available without rewriting an existing config. Explicit existing values of
`0` still win; this is intentional. Document the experimental artifact caveat rather than silently removing the
ability to disable merging.

### 3. Stage the rebuilt payload

```sh
cp "$WORK/rt-v3/libtotkrt.a" "$PKG/runtime/libtotkrt.a"
cp "$WORK/rt-v2/libtotkrt.a" "$PKG/runtime/libtotkrt-v2.a"
cp "$SRC/runtime/vk_exec.h" "$PKG/runtime/vk_exec.h"
```

Copy any other headers/install-side sources your adaptation actually changed. For this port, only `vk_exec.h`
changed among the packaged runtime headers; `cfg.c` and `vk_exec.c` are compiled into the archives, not shipped
as install-side sources. Include the guide as `SHRINE-FIXES.md`, update the example's defaults, and give `VERSION`
a custom suffix (the delivered build uses `0.5.1-rc1+3c5ae12-shrine`). Preserve the original profiles, generator,
compiler, dependencies and shader translator. A changed version identifies the custom installer to its step cache.

### 4. Repack, extract again, and verify before replacement

Use the pinned appimagetool and type-2 runtime described by `tools/make_release_linux.py`, not an arbitrary newer
release. For a repack of an existing image, its runtime prefix can be reused:

```sh
OFFSET=$("$WORK/original.AppImage" --appimage-offset)
head -c "$OFFSET" "$WORK/original.AppImage" > "$WORK/runtime-prefix"
# APPIMAGETOOL is the executable AppRun of an extracted, pinned appimagetool package.
ARCH=x86_64 "$APPIMAGETOOL" --no-appstream \
    --runtime-file "$WORK/runtime-prefix" "$PKG" "$WORK/patched.AppImage"
mkdir "$WORK/verify"
(cd "$WORK/verify" && "$WORK/patched.AppImage" --appimage-extract)
```

Compare every extracted file hash and symlink target against the staged payload, then compare the staged payload
against the original manifest. For the delivered 0.5.1 update, the entire change list was:

- `runtime/libtotkrt.a`, `runtime/libtotkrt-v2.a`, `runtime/vk_exec.h`
- `VERSION`, `totk.ini.example`, `SHRINE-FIXES.md`

Run the source regression check. Require the v2 ISA check to pass, and link a proof executable using matching
local guest/install-side objects and the rebuilt runtime with **whole-archive**. Run self-test/config/services
checks outside the live installation with an explicit disposable probe profile. Do not claim a full GPU test from
these checks. The delivered repack passed payload verification, v3 proof self-test/config checks and the v2 build's
ISA check; the existing service-probe coverage failure is recorded in the validation section below.

Only after verification, copy the new image to a temporary filename **beside the destination**, compare its hash
with `patched.AppImage`, preserve executable permissions, and atomically rename it over `IMAGE`. Keep
`original.AppImage` in the external work directory for rollback. Do not overwrite the only backup on a later run.

### 5. Use the updated installer without changing saves

Close the game, back up its executable outside the installation, then run the updated AppImage and select the
**existing installation directory**. Let setup rebuild/relink the game; do not copy an old executable over the
new runtime or select a different profile. Keep the normal launcher name and save path. Existing explicit
`barrier_merge = 0` or `shrine_barrier = 0` must be changed to `1` if enabling both is desired; otherwise leave the
rest of the settings alone. A future stock installer can replace this custom runtime, so repeat the port for each
new release rather than assuming the fix persists.

The November 23 repack replaced the user's existing 0.5.1-rc1 PGO AppImage in place, with SHA-256
`494c7f7c7f4f2a0f47bc88dca44aebc9422c2a65e72766ad437edf2d10d7953a`. It did not run setup or touch saves; the installed
game had already been patched separately. This hash identifies that particular artifact, not future rebuilds.

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

At initial publication, the main-based branch had not been built into a full game or installed. Subsequently, a
local adaptation onto the user's exact 0.5.1-rc1 base was built and installed at their request: all runtime objects
and install-side objects were rebuilt, and verified matching guest objects were reused. `--selftest`, `--probe-cfg`,
`--probe-kernel` and the adapted source regression check passed. `--probe-services` reported the same one failure
in both the stock and patched executables: `settings::detail::GetLanguageCode` was linked but absent from the
probe's coverage list (1 of 9,392 checks). It is not reported as a passing service probe.

The installation preserved save/profile paths and verified unchanged hashes of save/profile, mod and build-cache
files. Only the executable and the explicitly enabled settings were replaced; backups were kept outside the game
folder. The user subsequently ran the game; their visual report is recorded above. No GPU probes, Vulkan validation,
screenshots or controlled FPS/frametime comparisons were performed for this adaptation.

Before promoting the fix, follow [CONTRIBUTING.md](../CONTRIBUTING.md)'s runtime/rendering checks on the rebuilt
binary. Compare whole runs with `TOTK_SHRINE_BARRIER=0` and `1`, keeping `TOTK_BARRIER_MERGE=0`, the same shrine
save, camera/input, resolution, shadow settings and warmed shaders. Run at least two of each; report hardware,
driver, average FPS, lows/worst frames, slow frames and VRAM. Use `TOTK_TRACE=gfx` to compare layout transitions;
use performance tracing separately if tracing overhead affects timing. Test outdoor scenes, contact shadows,
transfers and chunked replay as well. Use `TOTK_VK_VALIDATION=1` and require no new validation errors. Frame dumps
intentionally fall back to the old path and therefore must not be used to measure the optimized path.
