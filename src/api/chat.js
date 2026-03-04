import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 20000, // 70秒，略大于后端最大超时时间（工作流60秒）
  headers: {
    'Content-Type': 'application/json'
  }
})

/**
 * 发送问答消息（后端会作为 Dify 工作流的 query 输入）
 * @param {string} message - 用户消息内容
 * @param {Array} history - 历史消息（可选）
 * @returns {Promise}
 */
export function sendChatMessage(message, history = []) {
  return api.post('/chat', { content: message, history })
}

export default api
