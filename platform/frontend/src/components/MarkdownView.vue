<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps({
  content: { type: String, default: '' },
})

// 全项目唯一的 v-html 使用点；样式在 App.vue 的全局段（scoped 命不中 v-html 内容）
const html = computed(() => renderMarkdown(props.content))
</script>
