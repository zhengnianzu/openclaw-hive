import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

export const useHarnessStore = defineStore('harness', () => {
  const types = ref([])               // ['openclaw','hermes',...] 不含 common
  const colors = ref({})              // { openclaw: '#409eff', ... }
  const labels = ref({})              // { openclaw: 'OpenClaw', ... }
  const fileLabels = ref({})          // { openclaw: 'Harness (openclaw.json)', ... }
  const agentConfig = ref({})         // { openclaw: 'openclaw.json', ... }
  const loaded = ref(false)
  let loadPromise = null

  async function load() {
    if (loaded.value) return
    if (loadPromise) return loadPromise
    loadPromise = (async () => {
      try {
        const meta = await api.get('/harness-meta')
        types.value = meta.types || []
        colors.value = meta.colors || {}
        labels.value = meta.labels || {}
        fileLabels.value = meta.fileLabels || {}
        agentConfig.value = meta.agentConfig || {}
        loaded.value = true
      } catch (e) {
        // 加载失败兜底: 空对象, 各 Vue 的 accessor 用默认色/原名
        loaded.value = true
      }
    })()
    return loadPromise
  }

  function color(t) { return colors.value[t] || '#909399' }
  function label(t) { return labels.value[t] || t || 'openclaw' }
  function fileLabel(t) { return fileLabels.value[t] || 'Harness 配置' }

  return { types, colors, labels, fileLabels, agentConfig, loaded, load, color, label, fileLabel }
})
