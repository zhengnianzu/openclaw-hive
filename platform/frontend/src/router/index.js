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
      { path: 'create', name: 'Create', component: () => import('../views/CreateInstance.vue') },
      { path: 'instance/:id', name: 'InstanceDetail', component: () => import('../views/InstanceDetail.vue') },
      { path: 'logs/:id', name: 'LogViewer', component: () => import('../views/LogViewer.vue') },
      { path: 'outputs/:id', name: 'OutputViewer', component: () => import('../views/OutputViewer.vue') },
      { path: 'registrations', name: 'Registrations', component: () => import('../views/TaskRegistrationList.vue') },
      { path: 'task-register', name: 'TaskRegister', component: () => import('../views/TaskRegister.vue') },
      { path: 'users', name: 'UserManagement', component: () => import('../views/UserManagement.vue') },
      { path: 'images', name: 'ImageManagement', component: () => import('../views/ImageManagement.vue') },
      { path: 'code-repos', name: 'CodeManagement', component: () => import('../views/CodeManagement.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')
  if (to.name !== 'Login' && to.name !== 'Register' && !token) next({ name: 'Login' })
  else next()
})

export default router
