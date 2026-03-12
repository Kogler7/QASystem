import { RefreshRight } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'knowledge-graph-update',
  title: '知识图谱更新组件库',
  desc: '对接知识库与图谱数据源，支持增量更新与实体关系维护，保障检索与推理的时效性。',
  icon: RefreshRight,
  color: 'var(--info)',
  route: 'knowledge-graph-update'
}

export const route = {
  path: 'knowledge-graph-update',
  name: 'knowledge-graph-update',
  component: () => import('./index.vue'),
  meta: { title: '知识图谱更新' }
}
