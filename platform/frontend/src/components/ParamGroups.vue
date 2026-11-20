<template>
  <div class="param-wrap">
    <!-- 左侧导航：一组一个条目，始终可见，点一下切换右侧内容 -->
    <div class="param-nav">
      <div v-for="g in groups" :key="g.key" class="pnav-item"
        :class="{ active: g.key === activeKey }" @click="activeKey = g.key">
        <div class="pnav-icon"><el-icon><component :is="g.icon" /></el-icon></div>
        <div class="pnav-body">
          <div class="pnav-t">{{ g.title }}</div>
          <div class="pnav-s" v-if="g.desc">{{ g.desc }}</div>
        </div>
        <el-icon class="pnav-arrow"><ArrowRight /></el-icon>
      </div>
    </div>
    <div class="param-body" v-if="activeGroup">
      <div class="param-body-head">
        <span class="code-mark">{{ activeGroup.title }}</span>
        <el-button v-if="copyable" size="small" @click="copyActive">
          <el-icon style="margin-right:4px"><CopyDocument /></el-icon>复制
        </el-button>
      </div>
      <el-table :data="activeGroup.rows" size="small" border class="param-table"
        empty-text="暂无数据">
        <el-table-column v-for="c in activeGroup.columns" :key="c.prop"
          :prop="c.prop" :label="c.label" :width="c.width"
          :min-width="c.minWidth || 120" show-overflow-tooltip>
          <template #default="{ row }">
            <!-- 列名对应的具名插槽，调用方想给某个字段加标签/格式化时
                 只覆盖那一列，其余列仍走默认的空值占位与布尔格式化 -->
            <slot :name="`cell-${c.prop}`" :row="row" :value="row[c.prop]">
              <span :class="{ 'cell-empty': isEmpty(row[c.prop]) }">
                {{ formatParam(row[c.prop]) }}
              </span>
            </slot>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowRight, CopyDocument } from '@element-plus/icons-vue'

const props = defineProps({
  // [{ key, title, icon, desc, columns: [{ prop, label, width?, minWidth? }], rows, data? }]
  groups: { type: Array, default: () => [] },
  defaultActive: { type: String, default: '' },
  // 右上角「复制」：复制当前分组的 data（没有则复制 rows）的 JSON
  copyable: { type: Boolean, default: false },
})

const activeKey = ref(props.defaultActive || props.groups[0]?.key || '')

// 分组是异步加载的：数据回来后若当前 key 不在新列表里（例如接口没返回该组），
// 回落到第一组，避免右侧空白。
watch(() => props.groups, (gs) => {
  if (!gs.some(g => g.key === activeKey.value)) activeKey.value = gs[0]?.key || ''
})

const activeGroup = computed(
  () => props.groups.find(g => g.key === activeKey.value) || props.groups[0],
)

const isEmpty = v => v === null || v === undefined || v === ''

function formatParam(v) {
  if (isEmpty(v)) return '—'
  if (typeof v === 'boolean') return v ? '开启' : '关闭'
  return v
}

async function copyActive() {
  const g = activeGroup.value
  if (!g) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(g.data ?? g.rows, null, 2))
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}
</script>

<style scoped>
.param-wrap { display: grid; grid-template-columns: 300px 1fr; gap: 16px; align-items: start; }
.param-nav { display: flex; flex-direction: column; gap: 8px; }
.pnav-item {
  display: flex; align-items: center; gap: 10px; cursor: pointer;
  padding: 11px 12px; border-radius: 8px;
  border: 1px solid var(--border-color);
  background: var(--bg-primary);
  transition: border-color .15s, background .15s;
}
.pnav-item:hover { border-color: #8b5cf6; }
.pnav-item.active {
  border-color: #8b5cf6;
  background: rgba(139, 92, 246, .08);
}
.pnav-icon {
  width: 32px; height: 32px; border-radius: 8px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center;
  background: rgba(139, 92, 246, .12); color: #8b5cf6;
}
.pnav-body { min-width: 0; flex: 1; }
.pnav-t { font-size: 13px; font-weight: 600; }
.pnav-s {
  font-size: 11px; color: var(--text-muted); margin-top: 1px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pnav-arrow { color: var(--text-muted); font-size: 12px; flex: 0 0 auto; }

.param-body { min-width: 0; }
.param-body-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px;
}
.code-mark { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.param-table { margin-top: 2px; }
.cell-empty { color: var(--text-muted); }

/* 窄屏下左栏收起，导航与内容上下堆叠 */
@media (max-width: 900px) {
  .param-wrap { grid-template-columns: 1fr; }
}
</style>
