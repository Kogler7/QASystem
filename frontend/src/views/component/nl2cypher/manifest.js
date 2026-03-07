import { DocumentCopy } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'nl2cypher',
  title: '向量转化存储与自然语言转 Cypher',
  desc: '将文本向量化存储，并根据自然语言问题与图谱 Schema 生成 Cypher 查询，支持 RAG 与图检索。',
  icon: DocumentCopy,
  color: 'var(--primary-500)',
  route: 'nl2cypher'
}

export const route = {
  path: 'nl2cypher',
  name: 'nl2cypher',
  component: () => import('./index.vue'),
  meta: { title: '向量转化存储与自然语言转 Cypher' }
}
