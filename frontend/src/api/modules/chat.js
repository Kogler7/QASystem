/**
 * 智能问答 - API
 */
import { request } from '../request'

/**
 * 发送问答消息（后端作为 Dify 工作流 query 输入）
 * @param {string} message - 用户消息内容
 * @param {Array} [history=[]] - 历史消息
 */
export function sendChatMessage(message, history = []) {
  return request.post('/chat', { content: message, history })
}
