import { request } from './request'
/**
 * 前端 API 统一导出
 * 按模块划分：智能问答、知识库、组件管理
 * 使用方式：import { sendChatMessage, knowledgeQuery, knowledgeExtract } from '@/api'
 */

// 认证与用户管理
export {
  loginApi,
  registerApi,
  getMeApi,
  listUsersApi,
  createUserApi,
  updateUserApi,
  deleteUserApi
} from './modules/auth'

// 智能问答
export { sendChatMessage } from './modules/chat'

// 知识库：文档管理、检索
export {
  getDocuments,
  checkDocumentNameExists,
  uploadDocument,
  deleteDocument,
  downloadDocument,
  knowledgeQuery
} from './modules/knowledge'

// 组件管理：知识抽取、知识图谱更新、自然语言转 Cypher 等
export { knowledgeExtract, extractHealthCheck, storeGraphToNeo4j, parseChunkedJsonExtract } from './modules/component'
export {
  kgUpdateHealthCheck,
  kgUpdateAdd,
  kgUpdateDelete,
  kgUpdateStatistics,
  kgCalculateConfidence,
  processSchemaOutput,   
  addFromComputed        
} from './modules/component/kg-update'
export { nl2cypherHealthCheck, nl2cypherGenerate } from './modules/component'
export { intentRecognitionHealthCheck, intentRecognitionRecognize } from './modules/component'
export { answerGenerationHealthCheck, answerGenerationAsk, answerGenerationAskVisualize, answerGenerationQueryGraph } from './modules/component'
export { documentPreprocHealthCheck, documentPreprocConvert, documentPreprocConvertToPdf, getAvailableModels } from './modules/component'
// -----------------------------------------------------------------------------
// 文本切片接口
// -----------------------------------------------------------------------------

/**
 * 切片：递归字符
 * @param {string} text
 */
export function nl2cypherSplitCharacter(text) {
  return request.post('/component/nl2cypher/split/character',
    { text: text },
    { timeout: 120000 }
  )
}

/**
 * 切片：递归字符
 * @param {string} text
 */
export function nl2cypherSplitRecursive(text) {
  return request.post('/component/nl2cypher/split/recursive', { text: text }, { timeout: 120000 })
}

/**
 * 切片：Markdown
 * @param {string} text
 */
export function nl2cypherSplitMarkdown(text, requestConfig = {}) {
  return request.post('/component/nl2cypher/split/markdown', { text: text }, { timeout: 120000, ...requestConfig })
}

/**
 * 切片：Python 代码
 * @param {string} text
 */
export function nl2cypherSplitPython(text) {
  return request.post('/component/nl2cypher/split/python', { text: text }, { timeout: 120000 })
}

export function nl2cypherText2vector(text, fileName = '') {
  return request.post('/component/nl2cypher/text2vector', { 
    text: text,
    source: fileName  // 添加文件名作为source
  }, { timeout: 120000 })
}

export const nl2cypherGetVectors = () => {
  return request({
    url: '/component/nl2cypher/vectors',
    method: 'get'
  })
}

export const mutiRetriever = () => {
  return request({
    url: '/component/nl2cypher/mutiRetriever',
    method: 'get'
  })
}
