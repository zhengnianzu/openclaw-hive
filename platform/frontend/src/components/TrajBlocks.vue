<template>
  <div class="traj-viewer" :class="{ 'is-scroll': scroll }">
    <div v-if="header || truncatedNote || anyMarkdown" class="traj-head">
      <span v-if="header">{{ header }}</span>
      <span v-if="truncatedNote" class="traj-trunc">{{ truncatedNote }}</span>
      <!-- 渲染/源码开关：只在真有可渲染的 markdown 块时出现。
           看 thinking 时有人就是想看原始字符（缩进、反引号的确切位置）。 -->
      <button v-if="anyMarkdown" class="traj-toggle" @click="raw = !raw">
        {{ raw ? '渲染 Markdown' : '查看源码' }}
      </button>
    </div>
    <div v-for="(b, i) in visibleBlocks" :key="i" class="traj-block"
      :class="`traj-${b.role || 'meta'}`">
      <div class="traj-block-head">
        <span class="traj-role">{{ roleLabel(b) }}</span>
        <span v-if="b.part_type === 'thinking'" class="traj-type">thinking</span>
        <span v-if="b.tool_name" class="traj-tool">{{ b.tool_name }}</span>
        <span v-if="b.part_type === 'toolCall'" class="traj-type">toolCall</span>
        <span v-if="b.isError || b.exitCode" class="traj-err">exit {{ b.exitCode ?? '?' }}</span>
        <el-icon v-if="i < visibleBlocks.length - 1" class="traj-fold" :size="14"
          @click="toggleBlock(i)">
          <ArrowDown v-if="!collapsed[i]" /><ArrowRight v-else />
        </el-icon>
      </div>
      <div v-if="!collapsed[i] && (b.content || b.args)" class="traj-body">
        <!-- 正文：text/thinking 是给人读的自然语言，常带 markdown
             （实测真实轨迹 137 块里 29 块含列表/行内代码/加粗），渲染后可读性差别很大。
             其余一律 <pre>：toolResult 正文是 ls 输出 / JSON，toolCall 的 args 是
             JSON —— markdown 会把 *、_、# 当语法吃掉，缩进也会被重排。
             判定在 utils/trajectory.js 的 isMarkdownBlock（那里有单测锁着）。 -->
        <MarkdownView v-if="b.content && !raw && isMarkdownBlock(b)"
          class="traj-md" :content="b.content" />
        <pre v-else-if="b.content" class="traj-content">{{ asText(b.content) }}</pre>
        <pre v-if="b.args" class="traj-args">{{ asText(b.args) }}</pre>
      </div>
    </div>

    <!-- 块数超过首屏上限时按批追加：单块正文可达十几万字符，
         一次全渲染会让浏览器卡死（与原 JSONL 预览同样的理由）。 -->
    <div v-if="hasMore" class="traj-more">
      <el-button size="small" @click="showMore">
        加载更多（已显示 {{ visibleBlocks.length }} / {{ blocks.length }}）
      </el-button>
    </div>
  </div>
</template>

<script setup>
// 轨迹气泡渲染器。两个来源共用：
//   1. 会话详情弹窗（InstanceDetail.vue）—— 块由后端 /deep/{traj}/detail 解析
//   2. 沙箱交付物分组视图（ArtifactWorkspace.vue）—— 块由 utils/trajectory.js 解析
// 两侧的块结构一致（{role, part_type, content, tool_name, args, isError, exitCode}），
// 所以这里不关心来源。
import { ref, computed, watch } from 'vue'
import { ArrowDown, ArrowRight } from '@element-plus/icons-vue'
import MarkdownView from './MarkdownView.vue'
import { isMarkdownBlock } from '../utils/trajectory.js'

const props = defineProps({
  blocks: { type: Array, default: () => [] },
  // 首屏渲染上限。详情弹窗原本一次全渲染（后端已按行/字节截断过），
  // 分组视图是任意文件直读，必须留这道闸。
  pageSize: { type: Number, default: 200 },
  // 文件被后端按字符截断时显示提示（分组视图用；详情弹窗的截断由后端
  // 直接插一个 part_type='truncated' 的块表达）。
  truncatedNote: { type: String, default: '' },
  header: { type: String, default: '' },
  // true = 自带 520px 内滚外框（详情弹窗原有观感）；
  // false = 交给外层容器滚（分组视图的预览面板已是滚动容器）。
  scroll: { type: Boolean, default: true },
})

const collapsed = ref({})
const limit = ref(props.pageSize)
// true = 全部按源码（<pre>）显示，不渲染 markdown
const raw = ref(false)

// 换文件/换任务时重置展开进度与折叠态，否则上一个文件的状态会带过来
watch(() => props.blocks, () => {
  collapsed.value = {}
  limit.value = props.pageSize
  raw.value = false
})

const visibleBlocks = computed(() => props.blocks.slice(0, limit.value))
const hasMore = computed(() => props.blocks.length > limit.value)

// 开关只在本屏真有可渲染块时才显示，免得给一堆纯 JSON 的轨迹加个没用的按钮
const anyMarkdown = computed(() => visibleBlocks.value.some(isMarkdownBlock))

function showMore() {
  limit.value = Math.min(props.blocks.length, limit.value + props.pageSize * 4)
}

function toggleBlock(i) {
  collapsed.value = { ...collapsed.value, [i]: !collapsed.value[i] }
}

function roleLabel(b) {
  if (!b) return '?'
  const map = {
    assistant: '助手',
    user: '用户',
    toolResult: '工具结果',
    thinking: '思考',
    toolCall: '工具调用',
    meta: '系统',
  }
  return map[b.role] || map[b.part_type] || b.role || b.part_type || '系统'
}

// content/args 可能是对象或数组（openclaw 的 toolCall arguments 实测有 dict 形态），
// 直接插值会渲染成 [object Object]。
function asText(v) {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}
</script>

<style scoped>
/* 样式从 InstanceDetail.vue 原「轨迹」子 tab 原样搬来（逐属性核对过），
   详情弹窗那侧必须是纯重构、观感零变化。
   .traj-viewer 的外框与 max-height 由 props.scroll 控制：详情弹窗沿用原来的
   520px 内滚；分组视图的预览面板自己已经是滚动容器，再套一层会双滚动条。 */
.traj-viewer.is-scroll {
  max-height: 520px; overflow-y: auto; border: 1px solid var(--border-color);
  border-radius: var(--radius-md); padding: 8px;
}
.traj-head {
  display: flex; gap: 14px; flex-wrap: wrap; align-items: center;
  font-size: 12px; color: #6e83a1; margin-bottom: 8px;
}
.traj-block { border-radius: 6px; margin-bottom: 6px; overflow: hidden; }
.traj-block:last-child { margin-bottom: 0; }
.traj-block-head {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 10px; font-size: 12px; min-height: 26px;
}
.traj-role { font-weight: 600; }
.traj-type { color: #909399; font-size: 11px; }
.traj-tool {
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  color: #6366f1; font-size: 11px;
}
.traj-err { color: #f56c6c; font-size: 11px; font-weight: 600; }
.traj-fold { margin-left: auto; cursor: pointer; color: #909399; }
.traj-fold:hover { color: var(--text-primary); }
.traj-body { padding: 2px 10px 8px; }
.traj-content, .traj-args {
  margin: 0; padding: 0; font-size: 12px; line-height: 1.55;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  white-space: pre-wrap; word-break: break-all;
}
.traj-args { color: var(--text-muted); margin-top: 4px; }
.traj-trunc { color: #d68e1b; }

/* markdown 正文：全局 .markdown-body 是按「文档正文」调的（14px/1.75、h1 26px、
   标题上下 1.6em），塞进 12px 的气泡里会显得特别大、行距也散。这里按气泡尺度
   压一档。用 :deep —— v-html 生成的节点命不中 scoped 选择器。 */
.traj-md { font-size: 12px; }
.traj-md :deep(.markdown-body),
.traj-md:deep(.markdown-body) { font-size: 12px; line-height: 1.6; }
.traj-md :deep(p) { margin: 0 0 0.6em; }
.traj-md :deep(ul), .traj-md :deep(ol) { margin: 0 0 0.6em; padding-left: 1.4em; }
.traj-md :deep(li) { margin: 0.1em 0; }
.traj-md :deep(h1), .traj-md :deep(h2),
.traj-md :deep(h3), .traj-md :deep(h4) { margin: 0.8em 0 0.4em; }
.traj-md :deep(h1) { font-size: 15px; }
.traj-md :deep(h2) { font-size: 14px; border-bottom: 0; padding-bottom: 0; }
.traj-md :deep(h3) { font-size: 13px; }
.traj-md :deep(h4) { font-size: 12px; }
/* 行内代码/代码块在气泡里也要跟着缩，否则比正文还大 */
.traj-md :deep(code) { font-size: 11px; }
.traj-md :deep(pre) { margin: 0 0 0.6em; max-height: 320px; overflow: auto; }
.traj-md :deep(pre code) { font-size: 11px; line-height: 1.5; }
.traj-md :deep(table) { font-size: 11px; }
.traj-md :deep(blockquote) { margin: 0 0 0.6em; }

/* 「查看源码 / 渲染 Markdown」开关：弱化成文字按钮，别抢正文注意力 */
.traj-toggle {
  margin-left: auto; border: 0; background: transparent; cursor: pointer;
  color: #6366f1; font-size: 12px; padding: 0;
}
.traj-toggle:hover { text-decoration: underline; }
.traj-assistant .traj-block-head { background: rgba(99, 102, 241, .10); }
.traj-assistant .traj-role { color: #6366f1; }
.traj-user .traj-block-head { background: rgba(16, 185, 129, .10); }
.traj-user .traj-role { color: #10b981; }
.traj-toolResult .traj-block-head { background: #f5f7fa; }
.traj-toolResult .traj-role { color: #606266; }
.traj-meta .traj-block-head { background: rgba(230, 162, 60, .10); }
.traj-meta .traj-role { color: #e6a23c; }
.traj-more { display: flex; justify-content: center; padding: 8px 0 4px; }
</style>
