import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const role = ref(localStorage.getItem('role') || '')

  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'admin' || role.value === 'operator')

  function setAuth(data) {
    token.value = data.access_token
    username.value = data.username
    role.value = data.role
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('username', data.username)
    localStorage.setItem('role', data.role)
  }

  function clearAuth() {
    token.value = ''
    username.value = ''
    role.value = ''
    localStorage.removeItem('token')
    localStorage.removeItem('username')
    localStorage.removeItem('role')
  }

  async function fetchMe() {
    try {
      const res = await api.get('/auth/me')
      username.value = res.username
      role.value = res.role
      localStorage.setItem('username', res.username)
      localStorage.setItem('role', res.role)
    } catch {
      clearAuth()
    }
  }

  return { token, username, role, isAdmin, isOperator, setAuth, clearAuth, fetchMe }
})
