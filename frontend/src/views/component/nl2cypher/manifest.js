import { DocumentCopy } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'nl2cypher',
  title: '文本向量化与切片组件库',
  desc: '将文本转为向量存储，不同逻辑下的文本切片，自然语言转Cypher。',
  icon: DocumentCopy,
  color: 'var(--primary-500)',
  route: 'nl2cypher'
}

export const route = {
  path: 'nl2cypher',
  name: 'nl2cypher',
  component: () => import('./index.vue'),
  meta: { title: '文本向量化与切片' }
}
