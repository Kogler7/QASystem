/**
 * 组件管理 - 菜单与路由配置
 * 新增组件时：在此添加菜单项与路由，再在 views/component/ 下添加对应页面即可
 */
import { Aim, Connection, RefreshRight, DataAnalysis, Reading } from '@element-plus/icons-vue'

/** 组件列表（主页面卡片），route 有值则点击跳转到 /component/{route} */
export const componentMenuItems = [
  {
    id: 'data-preprocessing',
    title: '多源数据预处理组件',
    desc: '从多个数据源获取数据，进行清洗、转换和标准化处理，将数据转换为统一格式，确保数据质量和一致性。',
    icon: DataAnalysis,
    color: 'var(--warning)'
    // route 未配置：暂未开放页面
  },
  {
    id: 'intent-recognition',
    title: '意图识别组件',
    desc: '对用户输入进行意图分类，识别查询、故障诊断、规范查询等类型，并路由到对应处理流程。',
    icon: Aim,
    color: 'var(--primary-500)'
  },
  {
    id: 'question-splitting',
    title: '问题切分组件',
    desc: '将复杂问题拆解为多个子问题，支持多轮追问与子问题并行检索，提升回答准确度。',
    icon: Connection,
    color: 'var(--success)'
  },
  {
    id: 'knowledge-graph-update',
    title: '知识图谱更新组件',
    desc: '对接知识库与图谱数据源，支持增量更新与实体关系维护，保障检索与推理的时效性。',
    icon: RefreshRight,
    color: 'var(--info)'
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
    component: () => import('./KnowledgeExtract.vue'),
    meta: { title: '知识抽取' }
  }
  // 后续新增示例：
  // { path: 'intent-recognition', name: 'intent-recognition', component: () => import('./IntentRecognition.vue'), meta: { title: '意图识别' } },
]
