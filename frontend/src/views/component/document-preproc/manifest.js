import { FolderOpened } from '@element-plus/icons-vue'

export const menuItem = {
  id: 'document-preproc',
  title: '文档预处理组件',
  desc: '多源文档（PDF、DOCX、Excel、HTML）转换为 Markdown/文本，支持 MinerU 解析与结构化输出。',
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
