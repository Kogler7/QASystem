import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000
})

/**
 * 获取文档列表
 */
export function getDocuments(params = {}) {
  return api.get('/knowledge/documents', { params })
}

/**
 * 上传文档
 * @param {FormData} formData - 包含 file 等字段
 */
export function uploadDocument(formData) {
  return api.post('/knowledge/documents', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

/**
 * 删除文档
 * @param {string|number} id - 文档 ID
 */
export function deleteDocument(id) {
  return api.delete(`/knowledge/documents/${id}`)
}

/**
 * 知识库检索
 * @param {string} query - 检索关键词
 * @param {object} options - 其他参数
 */
export function knowledgeQuery(query, options = {}) {
  return api.post('/knowledge/query', { query, ...options })
}

export default api
