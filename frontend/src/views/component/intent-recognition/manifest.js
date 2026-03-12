import { Aim } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'intent-recognition',
  title: '意图识别组件',
  desc: '对用户输入进行意图分类，识别查询、故障诊断、规范查询等类型，并路由到对应处理流程。',
  icon: Aim,
  color: 'var(--primary-500)',
  route: 'intent-recognition'
}

export const route = {
  path: 'intent-recognition',
  name: 'intent-recognition',
  component: () => import('./index.vue'),
  meta: { title: '意图识别' },
}
