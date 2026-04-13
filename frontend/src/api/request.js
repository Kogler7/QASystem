/**
 * 主站后端请求实例（/api -> 后端 8000）
 * 供 智能问答、知识库 等模块使用
 */
import axios from 'axios'
import router from '@/router'

const TOKEN_KEY = 'auth_token'

export const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' }
})

request.interceptors.request.use((config) => {
  const t = typeof localStorage !== 'undefined' ? localStorage.getItem(TOKEN_KEY) : ''
  if (t) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${t}`
  }
  return config
})

request.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(TOKEN_KEY)
      }
      const path = router.currentRoute.value?.path || ''
      if (path !== '/login' && path !== '/register') {
        const full = router.currentRoute.value?.fullPath || '/'
        router.replace({ path: '/login', query: { redirect: full !== '/login' ? full : undefined } })
      }
    }
    return Promise.reject(err)
  }
)

export default request
