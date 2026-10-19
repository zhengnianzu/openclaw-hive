import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('../views/Login.vue') },
  { path: '/register', name: 'Register', component: () => import('../views/Register.vue') },
  {
    path: '/',
    component: () => import('../views/Layout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/Dashboard.vue') },
      { path: 'analytics', name: 'Analytics', component: () => import('../views/Analytics.vue') },
      { path: 'create', name: 'Create', component: () => import('../views/CreateInstance.vue') },
      { path: 'queues', name: 'Queues', component: () => import('../views/Queues.vue') },
      { path: 'instance/:id', name: 'InstanceDetail', component: () => import('../views/InstanceDetail.vue') },
      // 日志/输出已合并进 InstanceDetail 的 tab，旧链接重定向过去
      { path: 'logs/:id', redirect: to => ({ path: `/instance/${to.params.id}`, query: { tab: 'logs' } }) },
      { path: 'outputs/:id', redirect: to => ({ path: `/instance/${to.params.id}`, query: { tab: 'outputs' } }) },
      { path: 'registrations', name: 'Registrations', component: () => import('../views/TaskRegistrationList.vue') },
      { path: 'task-register', name: 'TaskRegister', component: () => import('../views/TaskRegister.vue') },
      { path: 'users', name: 'UserManagement', component: () => import('../views/UserManagement.vue') },
      { path: 'images', name: 'ImageManagement', component: () => import('../views/ImageManagement.vue') },
      { path: 'code-repos', name: 'CodeManagement', component: () => import('../views/CodeManagement.vue') },
      { path: 'config-templates', name: 'ConfigTemplates', component: () => import('../views/ConfigTemplates.vue') },
      { path: 'harness-configs', name: 'HarnessConfigs', component: () => import('../views/HarnessConfigs.vue') },
      { path: 'sandbox-configs', name: 'SandboxConfigs', component: () => import('../views/SandboxConfigs.vue') },
      // 文档中心。参数名是 slug（= 文件名），不是自增 id。
      // wiki/new 必须排在 wiki/:slug 之前，否则 "new" 会被当成 slug
      { path: 'wiki', name: 'WikiList', component: () => import('../views/WikiList.vue') },
      { path: 'wiki/new', name: 'WikiNew', component: () => import('../views/WikiEdit.vue'), meta: { adminOnly: true } },
      { path: 'wiki/:slug', name: 'WikiDetail', component: () => import('../views/WikiDetail.vue') },
      { path: 'wiki/:slug/edit', name: 'WikiEdit', component: () => import('../views/WikiEdit.vue'), meta: { adminOnly: true } },
      { path: 'notifications', name: 'NotificationCenter', component: () => import('../views/NotificationCenter.vue') },
      { path: 'settings', name: 'SettingsOverview', component: () => import('../views/SettingsOverview.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')
  if (to.name !== 'Login' && to.name !== 'Register' && !token) {
    next({ name: 'Login' })
    return
  }
  // 角色守卫只是防误触（role 取自 localStorage），真正的边界在后端 require_admin。
  // 未登录时放过，交给上面的登录跳转。
  if (to.meta?.adminOnly && token && localStorage.getItem('role') !== 'admin') {
    next({ name: 'WikiList' })
    return
  }
  next()
})

export default router
