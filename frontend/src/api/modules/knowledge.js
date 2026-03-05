/**
 * 知识库 - API
 * 文档管理、知识检索
 */
import { request } from '../request'

/** 获取文档列表 */
export function getDocuments(params = {}) {
  return request.get('/knowledge/documents', { params })
}

/**
 * 上传文档
 * @param {FormData} formData - 包含 file 等字段
 */
export function uploadDocument(formData) {
  return request.post('/knowledge/documents', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

/** 删除文档 */
export function deleteDocument(id) {
  return request.delete(`/knowledge/documents/${id}`)
}

/**
 * 知识库检索
 * @param {string} query - 检索关键词
 * @param {object} [options] - 其他参数
 */
export function knowledgeQuery(query, options = {}) {
  return request.post('/knowledge/query', { query, ...options })
}
