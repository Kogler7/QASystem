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
    text: params.text?.trim() ?? ''
  }, { timeout: 6000 })
}

/**
 * 流式意图识别
 * @param {object} params - { text }
 * @returns {Promise<Response>} - 流式响应
 */
export function intentRecognitionStream(params) {
  return fetch('/api/component/intent-recognition/intent-recognition-stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ text: params.text?.trim() ?? '' })
  })
}

/**
 * 深度意图识别
 * @param {object} params - { text }
 */
export function intentRecognitionDeepRecognize(params) {
  return request.post('/component/intent-recognition/deep-recognize', { 
    text: params.text?.trim() ?? ''
  }, { timeout: 300000 })
}

/**
 * 获取计划
 * @param {object} params - { intent_name, domain }
 */
export function intentRecognitionGetPlan(params) {
  return request.post('/component/intent-recognition/get-plan', {
    intent_name: params.intent_name?.trim() ?? '',
    domain: params.domain?.trim() ?? ''
  }, { timeout: 30000000 })
}

/**
 * 获取工具列表
 * @param {object} params - { plan }
 */
export function intentRecognitionGetTools(params) {
  return request.post('/component/intent-recognition/get-tools', {
    plan: params.plan?.trim() ?? ''
  }, { timeout: 30000000 })
}
