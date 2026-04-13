import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { request } from '@/api/request'

const TOKEN_KEY = 'auth_token'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(typeof localStorage !== 'undefined' ? localStorage.getItem(TOKEN_KEY) || '' : '')
  const user = ref(null)

  const isAuthenticated = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')

  function setToken(t) {
    token.value = t || ''
    if (typeof localStorage === 'undefined') return
    if (t) localStorage.setItem(TOKEN_KEY, t)
    else localStorage.removeItem(TOKEN_KEY)
  }

  /** 与 localStorage 对齐（例如请求拦截器因 401 清除令牌后） */
  function syncTokenWithStorage() {
    if (typeof localStorage === 'undefined') return
    const t = localStorage.getItem(TOKEN_KEY) || ''
    token.value = t
    if (!t) user.value = null
  }

  async function fetchMe() {
    if (!token.value) {
      user.value = null
      return
    }
    const { data } = await request.get('/users/me')
    user.value = data
  }

  async function login(username, password) {
    const { data } = await request.post('/auth/login', { username, password })
    setToken(data.access_token)
    await fetchMe()
  }

  async function register(username, password) {
    await request.post('/auth/register', { username, password })
  }

  function logout() {
    setToken('')
    user.value = null
  }

  return {
    token,
    user,
    isAuthenticated,
    isAdmin,
    setToken,
    syncTokenWithStorage,
    fetchMe,
    login,
    register,
    logout
  }
})
