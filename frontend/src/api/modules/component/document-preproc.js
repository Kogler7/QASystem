/**
 * 组件管理 - 文档预处理 API
 * 请求主站后端 /api/component/document-preproc
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/document-preproc/health */
export function documentPreprocHealthCheck() {
  return request.get('/component/document-preproc/health', { timeout: 5000 })
}

/** 获取可用模型：GET /api/available_models */
export function getAvailableModels(requestConfig = {}) {
  return request.get('/available_models', requestConfig)
}

/**
 * 上传并转换单个文档（PDF/DOCX/Excel 等 -> Markdown/文本）
 * @param {File} file - 文件对象
 * @param {string} modelName - 可选的模型名称
 */
export function documentPreprocConvert(file, modelName = null, requestConfig = {}) {
  const form = new FormData()
  form.append('file', file)
  if (modelName) {
    form.append('model_name', modelName)
  }
  return request.post('/component/document-preproc/convert', form, {
    timeout: 300000,
    headers: { 'Content-Type': 'multipart/form-data' },
    ...requestConfig
  })
}


/**
 * 上传并将 .doc/.docx 转为 PDF（返回 PDF 二进制流）
 * @param {File} file
 */
export function documentPreprocConvertToPdf(file) {
  const form = new FormData()
  form.append('file', file)
  return request.post('/component/document-preproc/convert-to-pdf', form, {
    timeout: 300000,
    responseType: 'blob',
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}
