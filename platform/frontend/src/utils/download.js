import api from '../api'
import { ElMessage } from 'element-plus'

/**
 * 通过 axios 拉取二进制并触发浏览器下载。
 *
 * 为什么不用 window.open：请求要走 axios 才能带上 Authorization: Bearer，
 * 直接打开链接会被后端的 OAuth2 鉴权 401。
 *
 * 两个必须注意的点：
 *  - 响应拦截器会把响应 unwrap 成 res.data（api/index.js），blob 请求下 res.data
 *    就是 Blob 本身，但响应头随之丢失 —— 文件名只能由调用方传入。
 *  - 全局 axios timeout 是 30s，批量导出（上百个任务）会超时，这里覆盖为 120s。
 *
 * 失败时给固定中文文案：blob 请求下 err.response.data 是 Blob，拦截器里的
 * ElMessage.error(msg) 会显示成 "[object Blob]"。
 */
export async function downloadBlob(url, { method = 'get', data, params, filename } = {}) {
  let blob
  try {
    blob = await api.request({
      url,
      method,
      data,
      params,
      responseType: 'blob',
      timeout: 120000,
      silent: true,
    })
  } catch (err) {
    ElMessage.error('导出失败，请重试或减少导出数量')
    throw err
  }

  const objectUrl = window.URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename || 'export.xlsx'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  window.URL.revokeObjectURL(objectUrl)
}

/** 统一的导出文件名时间戳：YYYYMMDD_HHmm */
export function exportStamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`
}
