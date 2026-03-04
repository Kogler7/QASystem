import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('../views/HomeView.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('../views/ChatView.vue'),
    meta: { title: '智能问答' }
  },
  {
    path: '/component',
    name: 'component',
    component: () => import('../views/ComponentView.vue'),
    meta: { title: '组件管理' }
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('../views/knowledge/KnowledgeLayout.vue'),
    meta: { title: '知识库' },
    redirect: '/knowledge/index',
    children: [
      {
        path: 'index',
        name: 'knowledge-index',
        component: () => import('../views/knowledge/KnowledgeIndex.vue'),
        meta: { title: '知识库概览' }
      },
      {
        path: 'documents',
        name: 'knowledge-documents',
        component: () => import('../views/knowledge/DocumentManage.vue'),
        meta: { title: '文档管理' }
      },
      {
        path: 'query',
        name: 'knowledge-query',
        component: () => import('../views/knowledge/KnowledgeQuery.vue'),
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
    document.title = `${to.meta.title} - 轨道交通知识服务系统`
  }
  next()
})

export default router
