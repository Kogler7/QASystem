/**
 * 组件管理 - 菜单与路由配置
 *
 * 目录约定：
 * - views/component/{组件id}/index.vue  每个组件的页面（如 knowledge-extract、intent-recognition）
 * - api/modules/component/{组件id}.js    每个组件的 API
 *
 * 新增组件时：1) 在 componentMenuItems 中增加一项并设置 route；2) 在 componentRoutes 中增加路由；
 * 3) 在 views/component/ 下新建 {route}/index.vue；4) 在 api/modules/component/ 下新建 {route}.js 并在 index.js 中导出
 */
import { Aim, RefreshRight, Reading, DocumentCopy, ChatDotRound } from '@element-plus/icons-vue'

/** 组件列表（主页面卡片），route 有值则点击跳转到 /component/{route} */
export const componentMenuItems = [
  {
    id: 'intent-recognition',
    title: '意图识别组件',
    desc: '对用户输入进行意图分类，识别查询、故障诊断、规范查询等类型，并路由到对应处理流程。',
    icon: Aim,
    color: 'var(--primary-500)',
    route: 'intent-recognition'
  },
  {
    id: 'answer-generation',
    title: '答案生成组件',
    desc: '基于 Neo4j 知识图谱与问题求解器，对用户问题做问题定义、分解与方案获取，输出答案并可生成路径可视化。',
    icon: ChatDotRound,
    color: 'var(--success)',
    route: 'answer-generation'
  },
  {
    id: 'nl2cypher',
    title: '向量转化存储与自然语言转 Cypher',
    desc: '将文本向量化存储，并根据自然语言问题与图谱 Schema 生成 Cypher 查询，支持 RAG 与图检索。',
    icon: DocumentCopy,
    color: 'var(--primary-500)',
    route: 'nl2cypher'
  },
  {
    id: 'knowledge-graph-update',
    title: '知识图谱更新组件',
    desc: '对接知识库与图谱数据源，支持增量更新与实体关系维护，保障检索与推理的时效性。',
    icon: RefreshRight,
    color: 'var(--info)',
    route: 'knowledge-graph-update'
  },
  {
    id: 'knowledge-extract',
    title: '知识抽取组件',
    desc: '从文本中抽取指定主对象类型的实体与关系，支持本地模型与模板，可选择性写入 Neo4j 图谱。',
    icon: Reading,
    color: '#8B5CF6',
    route: 'knowledge-extract'
  }
]

/** 组件子路由（有独立页面的组件），与 componentMenuItems 中带 route 的项一一对应 */
export const componentRoutes = [
  {
    path: 'knowledge-extract',
    name: 'knowledge-extract',
    component: () => import('./knowledge-extract/index.vue'),
    meta: { title: '知识抽取' }
  },
  {
    path: 'knowledge-graph-update',
    name: 'knowledge-graph-update',
    component: () => import('./knowledge-graph-update/index.vue'),
    meta: { title: '知识图谱更新' }
  },
  {
    path: 'nl2cypher',
    name: 'nl2cypher',
    component: () => import('./nl2cypher/index.vue'),
    meta: { title: '向量转化存储与自然语言转 Cypher' }
  },
  {
    path: 'intent-recognition',
    name: 'intent-recognition',
    component: () => import('./intent-recognition/index.vue'),
    meta: { title: '意图识别' }
  },
  {
    path: 'answer-generation',
    name: 'answer-generation',
    component: () => import('./answer-generation/index.vue'),
    meta: { title: '答案生成' }
  }
]
