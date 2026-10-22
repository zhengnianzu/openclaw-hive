<template>
  <div class="md-editor-host" :class="{ 'is-readonly': readonly }">
    <div ref="root" class="milkdown-host" />
    <div v-if="loading" class="md-editor-loading">编辑器加载中…</div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import api from '../api'

const props = defineProps({
  modelValue: { type: String, default: '' },
  readonly: { type: Boolean, default: false },
  placeholder: { type: String, default: '开始写文档…' },
})
const emit = defineEmits(['update:modelValue'])

const root = ref(null)
const loading = ref(true)

let crepe = null
let replaceAllFn = null
// 回写防抖：markdownUpdated 每敲一个字都会触发，没必要每次都冒泡给父组件
let debounceTimer = null
// 我们自己最后一次 emit 出去的值，用于识别「父组件把同一个值传回来」的回环
let lastEmitted = props.modelValue

function scheduleEmit(markdown) {
  if (markdown === lastEmitted) return
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    lastEmitted = markdown
    emit('update:modelValue', markdown)
  }, 300)
}

async function uploadImage(file) {
  const fd = new FormData()
  fd.append('file', file)
  const res = await api.post('/wiki/assets', fd)
  return res.url
}

onMounted(async () => {
  try {
    // 编辑器体积较大（codemirror + katex 等），按需加载：
    // 只有真正进入编辑页时才下载这块 chunk，不影响其它页面首屏。
    const [{ Crepe }, { replaceAll }] = await Promise.all([
      import('@milkdown/crepe'),
      import('@milkdown/kit/utils'),
      import('@milkdown/crepe/theme/common/style.css'),
      import('@milkdown/crepe/theme/classic.css'),
    ])
    replaceAllFn = replaceAll

    crepe = new Crepe({
      root: root.value,
      defaultValue: props.modelValue || '',
      // AI 特性不需要，而且默认会去请求外部服务，明确关掉
      features: { [Crepe.Feature.AI]: false },
      featureConfigs: {
        [Crepe.Feature.ImageBlock]: {
          // 复用已有的图片上传接口，返回站内绝对路径
          onUpload: uploadImage,
          blockOnUpload: uploadImage,
        },
        [Crepe.Feature.Placeholder]: { text: props.placeholder },
      },
    })

    await crepe.create()

    if (props.readonly) crepe.setReadonly(true)

    crepe.on((listener) => {
      listener.markdownUpdated((_ctx, markdown) => {
        if (props.readonly) return
        scheduleEmit(markdown)
      })
    })
  } catch (e) {
    console.error('[MarkdownEditor] 初始化失败', e)
  } finally {
    loading.value = false
  }
})

// 外部改内容（例如切换文档）时才整体替换；
// 父组件把我们自己 emit 的值回传时（markdown === lastEmitted）直接忽略，
// 否则会和用户输入互相打架、光标乱跳。
watch(() => props.modelValue, (val) => {
  if (!crepe || !replaceAllFn || val === lastEmitted) return
  lastEmitted = val ?? ''
  crepe.editor.action(replaceAllFn(val || '', true))
})

watch(() => props.readonly, (val) => {
  crepe?.setReadonly(val)
})

onBeforeUnmount(async () => {
  clearTimeout(debounceTimer)
  if (crepe) {
    const instance = crepe
    crepe = null
    try { await instance.destroy() } catch { /* 忽略销毁期间的异常 */ }
  }
})
</script>

<style scoped>
.md-editor-host {
  position: relative;
  min-height: 460px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  /* 隔离 Crepe 的浮层（工具栏/顶部栏）与 Element Plus 的弹窗层级 */
  isolation: isolate;
}
.milkdown-host {
  min-height: 460px;
}
.md-editor-loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  font-size: 13px;
  background: var(--bg-secondary);
}
</style>
