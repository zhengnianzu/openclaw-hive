<template>
  <div class="jv">
    <div v-if="!state.ok" class="jv-warn">{{ state.error || '无法解析该文件' }}</div>

    <template v-else>
      <!-- 顶部统计 + 搜索：条数、解析失败数、截断提示都在这一行说清楚，
           不让用户对着一个过滤后的列表猜「为什么只有 12 条」。 -->
      <div class="jv-head">
        <div class="jv-stats">
          <span class="jv-total">共 {{ state.total.toLocaleString() }} 条</span>
          <span v-if="state.bad" class="jv-bad">{{ state.bad }} 条解析失败（按原文显示）</span>
          <span v-if="state.truncated" class="jv-bad">内容被截断，仅解析前 5000 条</span>
          <span v-if="truncated" class="jv-bad">{{ truncNote }}</span>
        </div>
        <el-input v-model="keyword" size="small" clearable class="jv-search"
          placeholder="⌕ 搜索记录（字段名或内容）" />
      </div>

      <div class="jv-main">
        <!-- 左：记录索引 -->
        <aside class="jv-list">
          <div class="jv-list-body">
            <div v-for="r in visibleRecords" :key="r.idx" class="jv-item"
              :class="{ active: r.idx === activeIdx, bad: !r.ok }"
              @click="activeIdx = r.idx">
              <div class="jv-item-top">
                <span class="jv-idx">#{{ r.idx + 1 }}</span>
                <span v-if="r.label" class="jv-label">{{ r.label }}</span>
                <span v-else-if="!r.ok" class="jv-label is-bad">非 JSON</span>
                <span class="jv-size">{{ fmtSize(r.bytes) }}</span>
              </div>
              <div class="jv-item-sum">{{ r.summary }}</div>
            </div>

            <div v-if="!filtered.length" class="jv-none">没有匹配的记录</div>
            <div v-else-if="hasMore" class="jv-more">
              <el-button size="small" @click="loadMore">
                加载更多（已显示 {{ visibleRecords.length }} / {{ filtered.length }}）
              </el-button>
            </div>
          </div>
        </aside>

        <!-- 右：单条记录详情。任何时刻只渲染一条，这是性能的关键。 -->
        <section class="jv-detail">
          <template v-if="active">
            <div class="jv-detail-head">
              <div class="jv-detail-title">
                <span class="jv-detail-idx">记录 #{{ active.idx + 1 }}</span>
                <span v-if="active.label" class="jv-label">{{ active.label }}</span>
                <span class="jv-size">{{ fmtSize(active.bytes) }}</span>
              </div>
              <div class="jv-detail-actions">
                <div class="jv-modes">
                  <button v-for="m in MODES" :key="m.id" class="jv-mode"
                    :class="{ active: mode === m.id }" @click="mode = m.id">{{ m.label }}</button>
                </div>
                <el-button size="small" @click="copyRecord">复制</el-button>
              </div>
            </div>

            <div class="jv-detail-body">
              <!-- 解析失败的行没有结构可言，只能给原文 -->
              <div v-if="!active.ok" class="jv-raw-note">
                该行不是合法 JSON，按原文显示。
              </div>
              <pre v-if="!active.ok" class="jv-raw">{{ active.raw }}</pre>

              <JsonTree v-else-if="mode === 'tree'" :value="active.value" :depth="0"
                class="jv-tree" />

              <!-- Raw：沿用已有的 highlightJson。内容来自本实例的沙箱产出、
                   且 highlightJson 先 escapeHtml 再加 span，v-html 在这里是安全的。 -->
              <pre v-else class="jv-raw is-json" v-html="activeHtml" />
            </div>
          </template>

          <div v-else class="jv-none">从左侧选择一条记录</div>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
// JSONL 浏览器：左侧记录索引 + 右侧单条详情（Tree / Raw）。
// 取代原来「一条一个 pre 块纵向平铺」的渲染 —— 那种形态在几千条、
// 单行十几万字符的 trajectory.jsonl 上既找不到东西也滚不动。
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import JsonTree from './JsonTree.vue'
import { parseJsonlRecords, highlightJson, fmtSize } from '../utils/artifacts'

const props = defineProps({
  text: { type: String, default: '' },
  // 后端 /content 的截断口径（文件级），与逐行解析上限是两回事，分开提示
  truncated: { type: Boolean, default: false },
  totalChars: { type: Number, default: 0 },
})

const MODES = [
  { id: 'tree', label: 'Tree（结构化）' },
  { id: 'raw', label: 'Raw（原始 JSON）' },
]

const LIST_PAGE = 100

const keyword = ref('')
const activeIdx = ref(0)
const listLimit = ref(LIST_PAGE)
const mode = ref('tree')

const state = computed(() => parseJsonlRecords(props.text))

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return state.value.records
  return state.value.records.filter(r => r.searchText.includes(kw))
})

const visibleRecords = computed(() => filtered.value.slice(0, listLimit.value))
const hasMore = computed(() => filtered.value.length > listLimit.value)

const active = computed(() =>
  state.value.records.find(r => r.idx === activeIdx.value) || null)

const activeHtml = computed(() => (active.value ? highlightJson(active.value.text) : ''))

const truncNote = computed(() => (props.totalChars
  ? `文件共 ${props.totalChars.toLocaleString()} 字符，仅加载前一部分，完整内容请下载`
  : '文件过大，仅加载部分内容，完整内容请下载'))

function loadMore() {
  listLimit.value = Math.min(filtered.value.length, listLimit.value + LIST_PAGE * 4)
}

async function copyRecord() {
  if (!active.value) return
  try {
    await navigator.clipboard.writeText(active.value.text)
    ElMessage.success('已复制该条记录')
  } catch {
    ElMessage.error('复制失败')
  }
}

// 搜索变化时回到第一页；若当前选中项被过滤掉了，顺移到结果里的第一条，
// 否则右栏会停在一条「列表里看不见」的记录上，让人以为搜索坏了。
watch(keyword, () => {
  listLimit.value = LIST_PAGE
  if (!filtered.value.length) return
  if (!filtered.value.some(r => r.idx === activeIdx.value)) {
    activeIdx.value = filtered.value[0].idx
  }
})

// 换文件时整体复位（父组件也会用 :key 重建，这里是双保险）
watch(() => props.text, () => {
  keyword.value = ''
  activeIdx.value = state.value.records.length ? state.value.records[0].idx : 0
  listLimit.value = LIST_PAGE
  mode.value = 'tree'
})
</script>

<style scoped>
/* 外层撑满 .aw-body（父组件为 jsonl 关掉了外层滚动），内部两栏各自滚动 */
.jv { display: flex; flex-direction: column; height: 100%; min-height: 0; }

.jv-head {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; border-bottom: 1px solid #ebeef5; flex-shrink: 0;
}
.jv-stats { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #6e83a1; }
.jv-total { font-weight: 600; color: #43506b; }
.jv-bad { color: #d68e1b; }
.jv-search { margin-left: auto; width: 260px; }

.jv-main { flex: 1 1 auto; min-height: 0; display: flex; }

/* 左栏：固定宽度，窄屏下再压一档。列表项三要素——序号+标签+大小、摘要行。 */
.jv-list {
  width: 272px; flex-shrink: 0; border-right: 1px solid #ebeef5;
  display: flex; flex-direction: column; min-height: 0; background: #fcfdff;
}
.jv-list-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 8px; }
.jv-item {
  padding: 8px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid transparent; margin-bottom: 6px;
}
.jv-item:hover { background: #f2f6fc; }
.jv-item.active { background: #eef4ff; border-color: #bcd4f6; }
.jv-item.bad .jv-idx { color: #d68e1b; }
.jv-item-top { display: flex; align-items: center; gap: 6px; }
.jv-idx { font: 600 12px/1.4 Consolas, Monaco, monospace; color: #43506b; }
.jv-label {
  font-size: 11px; line-height: 1.6; padding: 0 6px; border-radius: 4px;
  color: #6b50d6; background: #f0ecfd;
  max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.jv-label.is-bad { color: #b07d2f; background: #fdf3e3; }
.jv-size { margin-left: auto; font-size: 11px; color: #9aa7b8; flex-shrink: 0; }
.jv-item-sum {
  margin-top: 4px; font-size: 12px; line-height: 1.5; color: #7b899b;
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
.jv-more { display: flex; justify-content: center; padding: 6px 0 10px; }
.jv-none { padding: 24px 10px; text-align: center; color: #a0acbb; font-size: 12px; }

/* 右栏：详情。头部固定，正文自己滚。 */
.jv-detail { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }
.jv-detail-head {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; border-bottom: 1px solid #f0f2f5; flex-shrink: 0;
}
.jv-detail-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.jv-detail-idx { font-size: 14px; font-weight: 600; color: #303133; }
.jv-detail-title .jv-size { margin-left: 0; }
.jv-detail-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.jv-modes { display: flex; gap: 4px; }
.jv-mode {
  padding: 4px 10px; cursor: pointer; font-size: 12px; color: #6e83a1;
  background: #f5f7fa; border: 1px solid #e6ecf5; border-radius: 6px;
}
.jv-mode.active { color: #1f6feb; background: #eef4ff; border-color: #bcd4f6; }

.jv-detail-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 12px 14px; }
.jv-tree { padding-bottom: 12px; }

/* Raw：沿用 ArtifactWorkspace 的深色终端配色（scoped 样式不跨组件，重建一份）。
   高亮 class 由 highlightJson 注入，需要 :deep 才能命中。 */
.jv-raw {
  margin: 0; white-space: pre; overflow: auto;
  font: 12px/1.7 Consolas, Monaco, monospace; color: #c9d5e4;
  background: #142136; border-radius: 8px; padding: 16px;
}
.jv-raw:not(.is-json) { white-space: pre-wrap; word-break: break-word; }
.jv-raw :deep(.j-key) { color: #7fd1ff; }
.jv-raw :deep(.j-str) { color: #a5e075; }
.jv-raw :deep(.j-num) { color: #ffb454; }
.jv-raw :deep(.j-kw)  { color: #ff7b9c; }
.jv-raw-note {
  margin-bottom: 8px; padding: 8px 12px; border-radius: 6px; font-size: 12px;
  color: #8a6d1f; background: #fdf6e3; border: 1px solid #f2e3b3;
}
.jv-warn {
  margin: 12px 14px; padding: 8px 12px; border-radius: 6px; font-size: 12px;
  color: #8a6d1f; background: #fdf6e3; border: 1px solid #f2e3b3;
}

/* 窄栏（未全屏时预览栏本身就不宽）：退化成上下两段，列表限高，详情拿剩下的。 */
@media (max-width: 1400px) {
  .jv-main { flex-direction: column; }
  .jv-list {
    width: auto; max-height: 220px; border-right: 0;
    border-bottom: 1px solid #ebeef5;
  }
  .jv-search { width: 200px; }
}
</style>
