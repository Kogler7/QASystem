import { ChatDotRound } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'answer-generation',
  title: '答案生成组件',
  desc: '基于 Neo4j 知识图谱与问题求解器，对用户问题做问题定义、分解与方案获取，输出答案并可生成路径可视化。',
  icon: ChatDotRound,
  color: 'var(--success)',
  route: 'answer-generation'
}

export const route = {
  path: 'answer-generation',
  name: 'answer-generation',
  component: () => import('./index.vue'),
  meta: { title: '答案生成' }
}
