/**
 * 组件管理 - 知识图谱更新 API
 * 请求主站后端 /api/component/kg-update
 */

import request from '@/api/request'

/** 健康检查：GET /api/component/kg-update/health */
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
 * @param {Array<{ subject, predicate, object }>} triples
 */
export function kgUpdateDelete(triples) {
  return request.post('/component/kg-update/delete', { triples })
}

export function kgUpdateStatistics() {
  return request.get('/component/kg-update/statistics')
}

/** 计算置信度 */
export function kgCalculateConfidence(triples) {
  return request.post('/component/kg-update/calculate-confidence', { triples })
}
