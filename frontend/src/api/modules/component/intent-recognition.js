/**
 * 组件管理 - 意图识别 API
 * 请求主站后端 /api/component/intent-recognition
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/intent-recognition/health */
export function intentRecognitionHealthCheck() {
  return request.get('/component/intent-recognition/health', { timeout: 5000 })
}

/**
 * 意图识别
 * @param {object} params - { text }
 */
export function intentRecognitionRecognize(params) {
  return request.post('/component/intent-recognition/recognize', {
    problem_model: params.problem_model  // 直接传递对象
  }, { timeout: 6000 })
}


/**
 * 深度意图识别
 * @param {object} params - { problem_model }
 */
export function intentRecognitionDeepRecognize(params) {
  return request.post('/component/intent-recognition/deep-recognize', { 
    problem_model: params.problem_model  // 直接传递对象
  }, { timeout: 300000 })
}

/**
 * 获取计划
 * @param {object} params - { problem_model }
 */
export function intentRecognitionGetPlan(params) {
  return request.post('/component/problem-solving/plan', {
    problem_model: params.problem_model  // 直接传递对象
  }, { timeout: 30000000 })
}

/**
 * 获取工具列表
 * @param {object} params - { plan: list[dict] }
 */
export function intentRecognitionGetTools(params) {
  return request.post('/component/problem-solving/tool-match', {
    step: params.step  // 直接传递对象
  }, { timeout: 30000000 })
}
