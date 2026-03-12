import { FolderOpened } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'document-preproc',
  title: '多源数据解析组件库',
  desc: '统一接口处理PDF/Word/Excel等文档，支持模型选择与OCR增强，提取文本、表格、公式，为下游检索问答提供高质量结构化数据。',
  icon: FolderOpened,
  color: 'var(--warning)',
  route: 'document-preproc'
}

export const route = {
  path: 'document-preproc',
  name: 'document-preproc',
  component: () => import('./index.vue'),
  meta: { title: '文档预处理' }
}
