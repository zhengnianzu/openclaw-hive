<!-- 「验证模型」结果的内联展示。Harness配置 与 用户模拟配置 两处共用。

     报错一律原样展示（<pre> 保留换行与缩进）——上游网关的报错体里往往带着
     model_id 拼错、鉴权失败的原始 JSON，归纳过就没法排错了。 -->
<template>
  <el-alert v-if="result && result.ok" type="success" :closable="false"
    show-icon style="margin-top:8px">
    <template #title>
      验证通过（{{ result.used === 'anthropic' ? 'Anthropic Messages' : 'OpenAI Completions' }}）
    </template>
    <div style="font-size:12px;word-break:break-all">
      <div>请求地址：{{ result.request_url }}</div>
      <div v-if="result.reply">模型回复：{{ result.reply }}</div>
    </div>
  </el-alert>

  <el-alert v-else-if="result" type="error" :closable="false"
    show-icon style="margin-top:8px">
    <template #title>验证失败</template>
    <div style="font-size:12px">
      <div v-if="result.tried && result.tried.length"
        style="margin-bottom:4px;word-break:break-all">
        已尝试：{{ result.tried.join(' → ') }}
        <template v-if="result.request_url"> · {{ result.request_url }}</template>
      </div>
      <pre style="white-space:pre-wrap;word-break:break-all;margin:0;font-size:12px">{{ result.error }}</pre>
    </div>
  </el-alert>
</template>

<script setup>
defineProps({
  // null/undefined = 还没验证过，不渲染任何东西
  result: { type: Object, default: null },
})
</script>
