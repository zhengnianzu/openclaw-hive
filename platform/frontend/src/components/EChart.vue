<template>
  <div ref="el" class="echart" :style="{ height }"></div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent, LegendComponent, TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

// 按需注册，避免整包引入
echarts.use([
  BarChart, LineChart, PieChart,
  GridComponent, LegendComponent, TooltipComponent,
  CanvasRenderer,
])

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: String, default: '280px' },
})

const el = ref(null)
let chart = null
let observer = null

function render() {
  if (!chart) return
  // notMerge=true：数据集切换（例如清空后重建）时避免残留旧系列
  chart.setOption(props.option, true)
}

onMounted(() => {
  chart = echarts.init(el.value)
  render()
  // 侧边栏折叠 / 窗口缩放时重算尺寸
  observer = new ResizeObserver(() => chart && chart.resize())
  observer.observe(el.value)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  if (observer) observer.disconnect()
  if (chart) chart.dispose()
  chart = null
  observer = null
})
</script>

<style scoped>
.echart {
  width: 100%;
}
</style>
