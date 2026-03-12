/**
 * 组件管理 - 答案生成 API
 * 请求主站后端 /api/component/answer-generation
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/answer-generation/health */
export function answerGenerationHealthCheck() {
  return request.get('/component/answer-generation/health', { timeout: 5000 })
}

/**
 * 基于知识图谱提问
 * @param {object} params - question, detailed?
 */
export function answerGenerationAsk(params) {
  return request.post('/component/answer-generation/ask', {
    question: params.question?.trim() ?? '',
    detailed: params.detailed ?? false
  }, { timeout: 120000 })
}

/**
 * 提问并获取可视化
 */
export function answerGenerationAskVisualize(params) {
  return request.post('/component/answer-generation/ask/visualize', {
    question: params.question?.trim() ?? ''
  }, { timeout: 120000 })
}

/**
 * 查询知识图谱
 * @param {object} params - node_labels, limit?
 */
export function answerGenerationQueryGraph(params) {
  return request.post('/component/answer-generation/query-graph', {
    node_labels: params.node_labels ?? [],
    limit: params.limit ?? 20
  }, { timeout: 30000 })
}
