import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

export const useHarnessStore = defineStore('harness', () => {
  const types = ref([])               // ['openclaw','hermes',...] 不含 common
  const colors = ref({})              // { openclaw: '#409eff', ... }
  const labels = ref({})              // { openclaw: 'OpenClaw', ... }
  const fileLabels = ref({})          // { openclaw: 'Harness (openclaw.json)', ... }
  const agentConfig = ref({})         // { openclaw: 'openclaw.json', ... }
  const mdFiles = ref({})             // { openclaw: ['AGENTS.md', ...], ... }
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
        mdFiles.value = meta.mdFiles || {}
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

  function mdList(t) { return mdFiles.value[t] || [] }

  // 把 harness 的主色调成「浅底 + 同色深字」的柔和标签样式。
  //
  // 原来各页面用的是 el-tag 的 effect="dark"：主色直接铺满底色、文字固定白色。
  // 但这份配色表里既有 #00d084（亮绿）、#c0c4cc（浅灰）、#ff8c00（亮橙）这类
  // 高明度色，白字压上去对比度极低，一整列排下来非常刺眼。
  //
  // 这里改成：底色 = 主色 12% 透明度，文字 = 主色压暗后保证可读，边框 = 主色 35%。
  // 既保留「每个 harness 一眼可辨」的色相区分，又不会整片荧光。
  function tagStyle(t) {
    const hex = color(t)
    const rgb = hexToRgb(hex)
    if (!rgb) {
      // 非 #rrggbb 形态（配置写错等）：退回中性灰，保证仍可读
      return { color: '#606266', background: '#f4f4f5', borderColor: '#dcdfe6' }
    }
    const [r, g, b] = rgb
    return {
      color: darken(rgb, 0.35),
      background: `rgba(${r}, ${g}, ${b}, 0.12)`,
      borderColor: `rgba(${r}, ${g}, ${b}, 0.35)`,
    }
  }

  function hexToRgb(hex) {
    const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex || '').trim())
    if (!m) return null
    let h = m[1]
    if (h.length === 3) h = h.split('').map(c => c + c).join('')
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
  }

  // 压暗：按比例混入黑色。0.35 相当于保留 65% 亮度，深色系变化不大、
  // 浅色系（亮绿/浅灰）能压到白底上清楚可读。
  function darken(rgb, ratio) {
    const [r, g, b] = rgb.map(v => Math.round(v * (1 - ratio)))
    return `rgb(${r}, ${g}, ${b})`
  }

  return { types, colors, labels, fileLabels, agentConfig, mdFiles, loaded,
           load, color, label, fileLabel, mdList, tagStyle }
})
