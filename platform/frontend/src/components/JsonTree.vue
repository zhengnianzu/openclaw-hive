<template>
  <div class="jt">
    <div v-for="node in nodes" :key="node.key" class="jt-node">
      <!-- 容器节点：可折叠。行本身就是点击区，不额外放按钮 —— 层级深了
           满屏小按钮比内容还抢眼。 -->
      <template v-if="node.isContainer">
        <div class="jt-row jt-clickable" @click="toggle(node.key)">
          <span class="jt-chev">{{ isOpen(node.key) ? '▾' : '▸' }}</span>
          <span class="jt-key">"{{ node.key }}"</span><span class="jt-punc">:</span>
          <span v-if="isOpen(node.key)" class="jt-punc">
            {{ node.type === 'array' ? '[' : '{' }}
          </span>
          <span v-else class="jt-hint">{{ containerHint(node) }}</span>
        </div>
        <JsonTree v-if="isOpen(node.key)" :value="node.value" :depth="depth + 1"
          class="jt-children" />
        <div v-if="isOpen(node.key)" class="jt-row jt-close">
          <span class="jt-punc">{{ node.type === 'array' ? ']' : '}' }}</span>
        </div>
      </template>

      <!-- 超长字符串：默认只给预览框 + 展开按钮。把 248 KB 的 content 直接
           铺进 DOM 会让整个面板卡死，而用户多数时候只需要确认开头是什么。 -->
      <template v-else-if="node.isLong">
        <div class="jt-row">
          <span class="jt-chev jt-chev-empty" />
          <span class="jt-key">"{{ node.key }}"</span><span class="jt-punc">:</span>
          <span class="jt-type">string</span>
          <span class="jt-hint">({{ node.full.length.toLocaleString() }} chars)</span>
        </div>
        <div class="jt-long">
          <pre class="jt-long-body" :class="{ 'is-open': isOpen(node.key) }">{{
            isOpen(node.key) ? node.full : node.preview + '…'
          }}</pre>
          <button class="jt-expand" @click="toggle(node.key)">
            {{ isOpen(node.key) ? '› 收起' : `› 展开完整内容（${fmtSize(node.full.length)}）` }}
          </button>
        </div>
      </template>

      <!-- 普通叶子：键 + 按类型着色的值 -->
      <div v-else class="jt-row">
        <span class="jt-chev jt-chev-empty" />
        <span class="jt-key">"{{ node.key }}"</span><span class="jt-punc">:</span>
        <span class="jt-val" :class="`jt-${node.type}`">{{ leafText(node) }}</span>
      </div>
    </div>

    <div v-if="!nodes.length" class="jt-row jt-empty">
      <span class="jt-hint">{{ emptyHint }}</span>
    </div>
  </div>
</template>

<script setup>
// 递归的可折叠 JSON 结构树。只渲染「当前展开的那一层」：
// 折叠的子树完全不进 DOM，这是能在十几万字符的记录上保持流畅的前提。
import { ref, computed } from 'vue'
import { jsonTreeNodes, containerHint, fmtSize } from '../utils/artifacts'

const props = defineProps({
  value: { type: null, required: true },
  depth: { type: Number, default: 0 },
})

const nodes = computed(() => jsonTreeNodes(props.value))

// 默认展开第一层，更深的默认折叠：顶层字段要一眼看全，
// 嵌套结构则按需点开，否则一条复杂记录仍会糊满整屏。
const open = ref(new Set(
  props.depth === 0
    ? nodes.value.filter(n => n.isContainer).map(n => n.key)
    : [],
))

function isOpen(key) { return open.value.has(key) }

function toggle(key) {
  const s = new Set(open.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  open.value = s
}

function leafText(node) {
  if (node.type === 'string') return `"${node.value}"`
  if (node.type === 'null') return 'null'
  return String(node.value)
}

const emptyHint = computed(() => {
  if (Array.isArray(props.value)) return '[ ] 空数组'
  if (props.value && typeof props.value === 'object') return '{ } 空对象'
  return ''
})
</script>

<script>
export default { name: 'JsonTree' }
</script>

<style scoped>
/* 配色沿用 ArtifactWorkspace 的 JSON 高亮语义（键/串/数/关键字四色），
   但这里是浅底卡片而不是深色终端：结构树要长时间盯着读，浅底更省眼。 */
.jt { font: 12px/1.8 Consolas, Monaco, monospace; }
.jt-children { padding-left: 18px; border-left: 1px dashed #e3e9f2; margin-left: 5px; }
.jt-row { display: flex; align-items: baseline; gap: 4px; flex-wrap: wrap; }
.jt-clickable { cursor: pointer; border-radius: 4px; }
.jt-clickable:hover { background: #f2f6fc; }
.jt-chev { width: 10px; flex-shrink: 0; color: #9aa7b8; }
.jt-chev-empty { visibility: hidden; }
.jt-key { color: #1f6feb; }
.jt-punc { color: #7b899b; }
.jt-type {
  color: #6b50d6; background: #f0ecfd; border-radius: 4px;
  padding: 0 6px; font-size: 11px;
}
.jt-hint { color: #9aa7b8; font-size: 11px; }
.jt-val { overflow-wrap: anywhere; }
.jt-string { color: #1a7f37; }
.jt-number { color: #b8730b; }
.jt-boolean, .jt-null { color: #cf222e; }
.jt-close { color: #7b899b; }
.jt-empty { padding-left: 14px; }

/* 长字符串预览框：灰底、限高、可滚。展开后放宽到 420px 仍然限高 ——
   「展开」是让人读内容，不是让它把整页顶走。 */
.jt-long { margin: 4px 0 8px 14px; }
.jt-long-body {
  margin: 0; padding: 10px 12px; border-radius: 6px;
  background: #f6f8fb; border: 1px solid #e6ecf5; color: #43506b;
  white-space: pre-wrap; word-break: break-word;
  max-height: 140px; overflow: hidden;
}
.jt-long-body.is-open { max-height: 420px; overflow: auto; }
.jt-expand {
  margin-top: 6px; padding: 4px 10px; cursor: pointer;
  font: 11px/1.6 inherit; color: #1f6feb;
  background: #fff; border: 1px solid #cfe0f7; border-radius: 6px;
}
.jt-expand:hover { background: #f2f7ff; }
</style>
