/**
 * 组件管理 - 向量转化存储与自然语言转 Cypher API
 * 请求主站后端 /api/component/nl2cypher
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/nl2cypher/health */
export function nl2cypherHealthCheck() {
  return request.get('/component/nl2cypher/health', { timeout: 5000 })
}

/**
 * 自然语言转 Cypher
 * @param {object} params - question, graph_schema
 */
export function nl2cypherGenerate(params) {
  return request.post('/component/nl2cypher/generate', {
    question: params.question?.trim() ?? '',
    graph_schema: params.graph_schema?.trim() ?? ''
  }, { timeout: 120000 })
}
