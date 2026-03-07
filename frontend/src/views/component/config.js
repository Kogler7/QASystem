/**
 * 组件管理 - 菜单与路由配置（自动收集各子组件的 manifest.js）
 *
 * 目录约定：
 * - views/component/{组件id}/index.vue  每个组件的页面
 * - views/component/{组件id}/manifest.js 每个组件的菜单项与路由（新增组件时只需创建此文件，无需修改本 config）
 * - api/modules/component/{组件id}.js    每个组件的 API
 *
 * 新增组件：1) 在 views/component/ 下新建 {route}/ 目录；2) 添加 manifest.js 导出 menuItem、route；
 * 3) 添加 index.vue 页面；4) 在 api/modules/component/ 下新建 {route}.js 并在 index.js 中导出
 */
const modules = import.meta.glob('./*/manifest.js', { eager: true })

const componentMenuItems = []
const componentRoutes = []

for (const path of Object.keys(modules)) {
  const m = modules[path]
  const mod = m?.default || m
  if (mod?.menuItem) componentMenuItems.push(mod.menuItem)
  if (mod?.route) componentRoutes.push(mod.route)
}

// 按固定顺序展示（可调整 manifest 中的 order 字段扩展）
const order = [
  'document-preproc',
  'intent-recognition',
  'answer-generation',
  'nl2cypher',
  'knowledge-graph-update',
  'knowledge-extract'
]
componentMenuItems.sort((a, b) => {
  const ia = order.indexOf(a.id)
  const ib = order.indexOf(b.id)
  if (ia === -1 && ib === -1) return 0
  if (ia === -1) return 1
  if (ib === -1) return -1
  return ia - ib
})

export { componentMenuItems, componentRoutes }
