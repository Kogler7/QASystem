/**
 * 组件管理 - 知识抽取 API
 * 请求独立抽取服务（默认 localhost:8001），与主站 /api 分离
 */
import axios from 'axios'

const baseURL = import.meta.env.VITE_EXTRACT_API || 'http://localhost:8001'
const api = axios.create({
  baseURL,
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' }
})

/** 健康检查：探测抽取服务是否存活，GET /health */
export function extractHealthCheck() {
  return api.get('/health', { timeout: 5000 })
}

/**
 * 知识抽取
 * @param {object} params
 * @param {string} params.main_object - 主对象类型，必填
 * @param {string} params.text - 待抽取文本，必填
 * @param {boolean} [params.use_local=true] - 是否使用本地模型
 * @param {boolean} [params.use_templates=true] - 是否使用模板
 * @param {boolean} [params.store_to_neo4j=false] - 是否写入 Neo4j
 */
export function knowledgeExtract(params) {
  return api.post('/extract', {
    main_object: params.main_object,
    text: params.text,
    use_local: params.use_local ?? true,
    use_templates: params.use_templates ?? true,
    store_to_neo4j: params.store_to_neo4j ?? false
  })
}

export default api
