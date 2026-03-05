/**
 * 组件管理 - 知识抽取 API
 * 请求主站后端 /api/component/extract
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/extract/health */
export function extractHealthCheck() {
  return request.get('/component/extract/health', { timeout: 5000 })
}

/**
 * 知识抽取
 * @param {object} params - main_object, text, use_local, use_templates, store_to_neo4j
 */
export function knowledgeExtract(params) {
  return request.post('/component/extract', {
    main_object: params.main_object.trim(),
    text: params.text.trim(),
    use_local: params.use_local ?? true,
    use_templates: params.use_templates ?? true,
    store_to_neo4j: params.store_to_neo4j ?? false
  }, { timeout: 90000 })
}
