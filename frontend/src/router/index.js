import { createRouter, createWebHistory } from 'vue-router'
import { APP_TITLE } from '@/constants'
import { componentRoutes } from '@/views/component/config'

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '智能问答' }
  },
  {
    path: '/component',
    name: 'component',
    component: () => import('@/views/component/layout.vue'),
    meta: { title: '组件管理' },
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
    meta: { title: '知识库' },
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
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach((to, _from, next) => {
  if (to.meta?.title) {
    document.title = `${to.meta.title} - ${APP_TITLE}`
  }
  next()
})

export default router
