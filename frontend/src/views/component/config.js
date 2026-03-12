/**
 * 组件管理 - 菜单与路由配置
 */
// 手动导入answer-generation组件的manifest.js文件
import { menuItem, route } from './answer-generation/manifest.js'

const componentMenuItems = []
const componentRoutes = []

// 收集组件的菜单和路由
if (menuItem) componentMenuItems.push(menuItem)
if (route) componentRoutes.push(route)

export { componentMenuItems, componentRoutes }
