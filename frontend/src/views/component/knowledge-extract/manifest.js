import { Reading } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'knowledge-extract',
  title: '知识抽取组件',
  desc: '从文本中抽取指定主对象类型的实体与关系，支持本地模型与模板，可选择性写入 Neo4j 图谱。',
  icon: Reading,
  color: '#8B5CF6',
  route: 'knowledge-extract'
}

export const route = {
  path: 'knowledge-extract',
  name: 'knowledge-extract',
  component: () => import('./index.vue'),
  meta: { title: '知识抽取' }
}
