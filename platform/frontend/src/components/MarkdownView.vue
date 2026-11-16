<template>
  <div class="markdown-body" v-html="html" @click="onClick"></div>
  <Teleport to="body">
    <div v-if="zoomSrc" class="md-lightbox" @click="zoomSrc = ''">
      <img :src="zoomSrc" :alt="zoomAlt" />
      <button class="md-lightbox-close" type="button" aria-label="关闭">×</button>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps({
  content: { type: String, default: '' },
})

// 全项目唯一的 v-html 使用点；样式在 App.vue 的全局段（scoped 命不中 v-html 内容）
const html = computed(() => renderMarkdown(props.content))

// 正文里的图片有尺寸上限（见 App.vue 的 .markdown-body img），
// 点一下用原图尺寸放大看细节。
const zoomSrc = ref('')
const zoomAlt = ref('')

function onClick(e) {
  const img = e.target.closest?.('img')
  if (!img || !img.currentSrc) return
  zoomSrc.value = img.currentSrc
  zoomAlt.value = img.getAttribute('alt') || ''
}

function onKeydown(e) {
  if (e.key === 'Escape') zoomSrc.value = ''
}

watch(zoomSrc, (src) => {
  if (src) window.addEventListener('keydown', onKeydown)
  else window.removeEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.md-lightbox {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: rgba(15, 23, 42, 0.78);
  cursor: zoom-out;
}
.md-lightbox img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 4px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.4);
}
.md-lightbox-close {
  position: absolute;
  top: 16px;
  right: 20px;
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.15);
  color: #fff;
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
}
.md-lightbox-close:hover {
  background: rgba(255, 255, 255, 0.28);
}
</style>
