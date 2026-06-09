import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: () => import('../views/Layout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/Dashboard.vue') },
      { path: 'create', name: 'Create', component: () => import('../views/CreateInstance.vue') },
      { path: 'instance/:id', name: 'InstanceDetail', component: () => import('../views/InstanceDetail.vue') },
      { path: 'logs/:id', name: 'LogViewer', component: () => import('../views/LogViewer.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')
  if (to.name !== 'Login' && !token) next({ name: 'Login' })
  else next()
})

export default router
