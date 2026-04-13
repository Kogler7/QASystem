import { createRouter, createWebHistory } from 'vue-router'
import { APP_TITLE } from '@/constants'
import { useAuthStore } from '@/stores/auth'
import { componentRoutes } from '@/views/component/config'

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', guestOnly: true }
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { title: '注册', guestOnly: true }
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '智能问答', requiresAuth: true }
  },
  {
    path: '/component',
    name: 'component',
    component: () => import('@/views/component/layout.vue'),
    meta: { title: '组件管理', requiresAuth: true, requiresAdmin: true },
    redirect: '/component/index',
    children: [
      {
        path: 'index',
        name: 'component-index',
        component: () => import('@/views/component/index.vue'),
        meta: { title: '组件管理' }
      },
      ...componentRoutes
    ]
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/knowledge/KnowledgeLayout.vue'),
    meta: { title: '知识库', requiresAuth: true, requiresAdmin: true },
    redirect: '/knowledge/index',
    children: [
      {
        path: 'index',
        name: 'knowledge-index',
        component: () => import('@/views/knowledge/KnowledgeIndex.vue'),
        meta: { title: '知识库概览' }
      },
      {
        path: 'list',
        name: 'knowledge-documents-list',
        component: () => import('@/views/knowledge/documents/DocumentList.vue'),
        meta: { title: '文档列表' }
      },
      {
        path: 'upload',
        name: 'knowledge-documents-upload',
        component: () => import('@/views/knowledge/documents/DocumentUpload.vue'),
        meta: { title: '文档上传' }
      },
      {
        path: 'documents',
        redirect: '/knowledge/list'
      },
      {
        path: 'documents/list',
        redirect: '/knowledge/list'
      },
      {
        path: 'documents/upload',
        redirect: '/knowledge/upload'
      },
      {
        path: 'query',
        name: 'knowledge-query',
        component: () => import('@/views/knowledge/KnowledgeQuery.vue'),
        meta: { title: '知识检索' }
      }
    ]
  },
  {
    path: '/admin/users',
    name: 'admin-users',
    component: () => import('@/views/admin/UserManageView.vue'),
    meta: { title: '用户管理', requiresAuth: true, requiresAdmin: true }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async (to, _from, next) => {
  if (to.meta?.title) {
    document.title = `${to.meta.title} - ${APP_TITLE}`
  }

  const auth = useAuthStore()
  auth.syncTokenWithStorage()

  if (to.meta?.guestOnly && auth.isAuthenticated) {
    return next(auth.isAdmin ? '/component' : '/chat')
  }

  try {
    if (auth.token && !auth.user) {
      await auth.fetchMe()
    }
  } catch {
    auth.logout()
  }

  const needsAuth = to.matched.some((r) => r.meta?.requiresAuth)
  if (needsAuth && !auth.isAuthenticated) {
    return next({ path: '/login', query: { redirect: to.fullPath } })
  }

  const needsAdmin = to.matched.some((r) => r.meta?.requiresAdmin)
  if (needsAdmin && !auth.isAdmin) {
    return next({ path: '/chat', query: { denied: '1' } })
  }

  next()
})

export default router
