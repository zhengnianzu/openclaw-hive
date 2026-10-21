<template>
  <div class="diff-view">
    <div v-if="!lines.length" class="diff-empty">无差异</div>
    <div v-for="(l, i) in lines" :key="i" :class="['diff-line', l.type]">
      <span class="ln">{{ l.old_no ?? '' }}</span>
      <span class="ln">{{ l.new_no ?? '' }}</span>
      <span class="mark">{{ mark(l.type) }}</span>
      <span class="txt">{{ l.text || ' ' }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // 服务端 /diff 返回的 lines: [{type: add|del|ctx|hunk|header, text, old_no, new_no}]
  lines: { type: Array, default: () => [] },
})

function mark(type) {
  return { add: '+', del: '-', hunk: '', header: '' }[type] ?? ' '
}
</script>

<style scoped>
.diff-view {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.65;
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  overflow: auto;
  max-height: 60vh;
}
.diff-empty { padding: 24px; text-align: center; color: var(--text-muted); }
.diff-line { display: flex; white-space: pre; padding: 0 8px; }
.diff-line.add { background: #f0fdf4; }
.diff-line.del { background: #fef2f2; }
.diff-line.hunk { background: #eff6ff; color: #1d4ed8; }
.diff-line.header { color: var(--text-muted); }
.ln {
  width: 40px; flex-shrink: 0; text-align: right; padding-right: 8px;
  color: var(--text-muted); user-select: none;
}
.mark { width: 14px; flex-shrink: 0; color: var(--text-muted); }
.txt { flex: 1; overflow-x: auto; }
</style>
