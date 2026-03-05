/**
 * 组件管理 - 文档预处理 API
 * 请求主站后端 /api/component/document-preproc
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/document-preproc/health */
export function documentPreprocHealthCheck() {
  return request.get('/component/document-preproc/health', { timeout: 5000 })
}

/**
 * 上传并转换单个文档（PDF/DOCX/Excel 等 -> Markdown/文本）
 * @param {File} file - 文件对象
 */
export function documentPreprocConvert(file) {
  const form = new FormData()
  form.append('file', file)
  return request.post('/component/document-preproc/convert', form, {
    timeout: 300000,
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}
