/**
 * 组件管理 - 知识图谱更新 API
 * 请求主站后端 /api/component/kg-update
 */

import request from '@/api/request'

/** 健康检查 */
export function kgUpdateHealthCheck() {
  return request.get('/component/kg-update/health')
}

/**
 * 批量添加三元组
 * @param {Array<{ subject, predicate, object, confidence }>} triples
 */
export function kgUpdateAdd(triples) {
  return request.post('/component/kg-update/add', { triples })
}

/**
 * 批量删除三元组
 */
export function kgUpdateDelete(triples) {
  return request.post('/component/kg-update/delete', { triples })
}

/** 获取图谱统计 */
export function kgUpdateStatistics() {
  return request.get('/component/kg-update/statistics')
}

/** 计算简单三元组的置信度 */
export function kgCalculateConfidence(triples) {
  return request.post('/component/kg-update/calculate-confidence', { triples })
}

/** 处理 schema_mapper 输出的 JSON，提取关系并计算置信度（不入库） */
export function processSchemaOutput(data, confidenceThreshold = 0.7, requestConfig = {}) {
  return request.post('/component/kg-update/process-schema-output', {
    data: data,
    confidence_threshold: confidenceThreshold
  }, requestConfig)
}

/** 从计算结果入库 */
export function addFromComputed(relationsHigh, fullRelations, predictions, confidenceThreshold = 0.7, requestConfig = {}) {
  return request.post('/component/kg-update/add-from-computed', {
    relations_high: relationsHigh,
    full_relations: fullRelations,
    predictions: predictions,
    confidence_threshold: confidenceThreshold
  }, requestConfig)
}