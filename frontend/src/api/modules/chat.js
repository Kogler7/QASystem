/**
 * 智能问答 - API
 */
import { request } from '../request'

/**
 * 发送问答消息（后端作为 Dify 工作流 query 输入）
 * @param {string} message - 用户消息内容
 * @param {Array} [history=[]] - 历史消息
 * @param {string} [conversationId=''] - 会话ID（用于对话型应用保持上下文）
 */
export function sendChatMessage(message, history = [], conversationId = '') {
  const payload = { content: message, history }
  if (conversationId) {
    payload.context = { conversation_id: conversationId }
  }
  return request.post('/chat', payload)
}
