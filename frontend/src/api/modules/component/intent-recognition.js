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
  }, { timeout: 60000 })
}
