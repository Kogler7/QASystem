/**
 * 知识库 - API
 * 文档管理、知识检索
 */
import { request } from '../request'

/** 获取文档列表 */
export function getDocuments(params = {}) {
  return request.get('/knowledge/documents', { params })
}

/** 检查文档文件名是否已在知识库中存在（与上传接口查重规则一致） */
export function checkDocumentNameExists(name, requestConfig = {}) {
  return request.get('/knowledge/documents/exists', {
    params: { name },
    ...requestConfig
  })
}

/**
 * 上传文档
 * @param {FormData} formData - 包含 file 等字段
 */
export function uploadDocument(formData, requestConfig = {}) {
  return request.post('/knowledge/documents', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    ...requestConfig
  })
}

/** 删除文档 */
export function deleteDocument(id) {
  return request.delete(`/knowledge/documents/${id}`)
}

/** 下载文档（返回 Blob） */
export function downloadDocument(id) {
  return request.get(`/knowledge/documents/${id}/download`, {
    responseType: 'blob',
    timeout: 120000
  })
}

/**
 * 知识库检索
 * @param {string} query - 检索关键词
 * @param {object} [options] - 其他参数
 */
export function knowledgeQuery(query, options = {}) {
  return request.post('/knowledge/query', { query, ...options })
}
