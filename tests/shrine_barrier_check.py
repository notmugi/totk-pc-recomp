"""Compile the actual barrier helpers against Vulkan/VkExec headers, without a GPU or game data.

    python3 tests/shrine_barrier_check.py
    python3 tests/shrine_barrier_check.py --include /path/to/vma

Requires Clang (or CC), Vulkan headers and vk_mem_alloc.h. Outputs stay in build/test/shrine-barrier.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "vk_exec.h"
int vk_dump_armed;
static unsigned calls, transitions, flushes;
static int alias_reset, pending_ub;
void vk_exec_pass_end(VkExec *ex) {
    if (!ex->in_pass) return;
    ex->in_pass = 0;
    ex->recorded++;
    if (alias_reset) ex->nfbl = 0, ex->fbl_serial++;
}
void vk_exec_ub_flush(VkExec *ex) {
    assert(!ex->in_pass);
    flushes++;
    if (pending_ub) ex->recorded++, pending_ub = 0;
}
VKAPI_ATTR void VKAPI_CALL vkCmdPipelineBarrier2(VkCommandBuffer cb, const VkDependencyInfo *d) {
    (void)cb;
    assert(flushes);
    assert(d->memoryBarrierCount == 1);
    const VkMemoryBarrier2 *m = d->pMemoryBarriers;
    assert(m->srcStageMask == VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT);
    assert(m->dstStageMask == VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT);
    assert(m->srcAccessMask == VK_ACCESS_2_MEMORY_WRITE_BIT);
    assert(m->dstAccessMask == (VK_ACCESS_2_MEMORY_READ_BIT | VK_ACCESS_2_MEMORY_WRITE_BIT));
    assert(!d->dependencyFlags); /* not a by-region dependency */
    for (unsigned i = 0; i < d->imageMemoryBarrierCount; i++) {
        const VkImageMemoryBarrier2 *b = &d->pImageMemoryBarriers[i];
        assert(b->srcStageMask == m->srcStageMask && b->dstStageMask == m->dstStageMask);
        assert(b->srcAccessMask == m->srcAccessMask && b->dstAccessMask == m->dstAccessMask);
        assert(b->oldLayout == VK_IMAGE_LAYOUT_ATTACHMENT_FEEDBACK_LOOP_OPTIMAL_EXT);
        assert(b->newLayout == VK_IMAGE_LAYOUT_GENERAL);
        assert(b->srcQueueFamilyIndex == VK_QUEUE_FAMILY_IGNORED);
        assert(b->dstQueueFamilyIndex == VK_QUEUE_FAMILY_IGNORED);
        assert(b->subresourceRange.levelCount == VK_REMAINING_MIP_LEVELS);
        assert(b->subresourceRange.layerCount == VK_REMAINING_ARRAY_LAYERS);
    }
    transitions += d->imageMemoryBarrierCount;
    calls++;
}
'''

CHECKS = r'''
static VkExec setup(int count) {
    VkExec ex = {0};
    ex.in_pass = 1; ex.recorded = 5; ex.barrier_at = -1; ex.nfbl = count;
    ex.retain_feedback = 1; ex.pass_storage = ex.pass_merged = 1; ex.fbl_serial = 7;
    for (int i = 0; i < count; i++) {
        ex.fbl_img[i] = (VkImage)(uintptr_t)(i + 1);
        ex.fbl_aspect[i] = i == 8 ? VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT : VK_IMAGE_ASPECT_COLOR_BIT;
    }
    calls = transitions = flushes = 0;
    alias_reset = pending_ub = vk_dump_armed = 0;
    return ex;
}
int main(void) {
    for (int count = 0; count <= 9; count++) {
        VkExec ex = setup(count);
        guest_barrier(&ex);
        assert(!ex.in_pass && ex.nfbl == count && calls == 1 && transitions == 0);
        assert(!ex.pass_storage && !ex.pass_merged && ex.fbl_serial == 7);
        assert(ex.barrier_at == ex.recorded && ex.fr.fb_layouts == 0);
        for (int i = 0; i < count; i++) {
            assert(ex.fbl_img[i] == (VkImage)(uintptr_t)(i + 1));
            assert(ex.fbl_aspect[i] == (i == 8 ? VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT : VK_IMAGE_ASPECT_COLOR_BIT));
        }
        guest_barrier(&ex);
        assert(calls == 1); /* no intervening commands */
        pending_ub = 1;
        guest_barrier(&ex);
        assert(calls == 2 && !pending_ub); /* flushed buffer copies must not elide the dependency */
        ex.recorded++;
        barrier_to(&ex, ex.fbl_img, ex.fbl_aspect, ex.nfbl); /* next pass, same targets */
        assert(calls == 3 && transitions == 0 && ex.fbl_serial == 7);
        vk_exec_barrier(&ex); /* transfers, compute, end of submission */
        assert(ex.nfbl == 0 && transitions == (unsigned)count);
        assert(calls == (count ? 4u : 3u));
        assert(ex.fbl_serial == (count ? 8u : 7u));
        assert(ex.fr.fb_layouts == (unsigned)count);
    }
    VkExec ex = setup(2);
    ex.retain_feedback = 0;
    guest_barrier(&ex);
    assert(ex.nfbl == 0 && transitions == 2 && calls == 1 && ex.fbl_serial == 8);
    ex = setup(2); vk_dump_armed = 1;
    guest_barrier(&ex);
    assert(ex.nfbl == 0 && transitions == 2 && calls == 1 && ex.fbl_serial == 8);
    ex = setup(2); alias_reset = 1;
    guest_barrier(&ex);
    assert(ex.nfbl == 0 && transitions == 0 && calls == 1 && ex.fbl_serial == 8);
    ex = setup(2);
    guest_barrier(&ex);
    VkImage want = ex.fbl_img[1];
    VkImageAspectFlags aspect = ex.fbl_aspect[1];
    barrier_to(&ex, &want, &aspect, 1); /* changed render targets drop only the old image */
    assert(ex.nfbl == 1 && ex.fbl_img[0] == want && transitions == 1 && ex.fbl_serial == 8);
    puts("PASS: retained layouts, full dependency, merge bookkeeping, reset, switch, dump, alias ordering, UB flush");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include', action='append', default=[], help='extra header directory (repeatable)')
    args = parser.parse_args()
    src = (ROOT / 'runtime/vk_exec.c').read_text(encoding='utf-8')
    case = src[src.index('case OP_BARRIER:'):src.index('case OP_DRAW:', src.index('case OP_BARRIER:'))]
    assert case.index('barrier_merges(') < case.index('guest_barrier(ex);')
    assert 'vk_exec_barrier(ex);' not in case
    assert 'ex->retain_feedback = (uint8_t)cfg_bool("TOTK_SHRINE_BARRIER");' in src
    contact = (ROOT / 'runtime/vk_contact.c').read_text(encoding='utf-8')
    start = contact.index('void vk_contact_exec(')
    assert contact.index('vk_exec_barrier(ex);', start) < contact.index('vkCmdDispatch(', start)
    helpers = src[src.index('static int has_image('):src.index('void vk_exec_pass_begin(')]
    work = ROOT / 'build/test/shrine-barrier'
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'check.c'
    source.write_text(HARNESS + helpers + CHECKS, encoding='utf-8')
    exe = work / ('check.exe' if os.name == 'nt' else 'check')
    includes = [ROOT / 'runtime', ROOT / 'toolchain/vma', *args.include]
    command = shlex.split(os.environ.get('CC', 'clang')) + ['-std=gnu11', '-Wall', '-Wextra', '-Werror']
    for directory in includes:
        command += ['-I', str(directory)]
    env = dict(os.environ, TMPDIR=str(work), TMP=str(work), TEMP=str(work))
    for opt in ('-O0', '-O2'):
        subprocess.run(command + [opt, str(source), '-o', str(exe)], env=env, check=True)
        subprocess.run([str(exe)], env=env, check=True)
    print('PASS: actual OP_BARRIER routing and existing contact-compute GENERAL reset')


if __name__ == '__main__':
    main()
