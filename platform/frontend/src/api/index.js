import axios from 'axios'
import { ElMessage } from 'element-plus'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    // 带 silent 标记的请求失败不弹全局提示（如会话详情首检 409 → 走触发下载分支）
    if (err.config?.silent) return Promise.reject(err)
    const msg = err.response?.data?.detail || err.message
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      localStorage.removeItem('role')
      window.location.hash = '#/login'
    }
    ElMessage.error(msg)
    return Promise.reject(err)
  },
)

export default api
