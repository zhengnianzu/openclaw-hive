import api from './index'

// 运行时从后端读取 OBS 桶配置，避免前端硬编码桶名。
// 后端 /api/obs/config 返回 { bucket: 'obs://xxx/' }（始终以 / 结尾）。

const FALLBACK_BUCKET = 'obs://rl-agentdata/'

const state = {
  bucket: FALLBACK_BUCKET,
  loaded: false,
}

let inflight = null

export async function loadObsConfig() {
  if (state.loaded) return state.bucket
  if (inflight) return inflight
  inflight = api
    .get('/obs/config')
    .then((res) => {
      if (res && res.bucket) state.bucket = res.bucket
      state.loaded = true
      return state.bucket
    })
    .catch(() => {
      // 拉取失败时退回默认桶，保证界面可用
      state.loaded = true
      return state.bucket
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

// 桶根，始终以 / 结尾，如 obs://xxx/
export function obsBucket() {
  return state.bucket
}

// 去掉桶前缀，得到相对路径（无前导/末尾斜杠）
export function stripBucket(p) {
  if (!p) return ''
  const b = state.bucket
  let rel = p
  if (rel.startsWith(b)) rel = rel.slice(b.length)
  else rel = rel.replace(/^obs:\/\/[^/]+\/?/, '')
  return rel.replace(/^\/+|\/+$/g, '')
}

// 拼接桶根 + 相对路径，返回以 / 结尾的目录路径
export function joinBucket(rel) {
  const clean = (rel || '').replace(/^\/+|\/+$/g, '')
  return state.bucket + (clean ? clean + '/' : '')
}
