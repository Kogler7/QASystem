/**
 * 前端 API 统一导出
 * 按模块划分：智能问答、知识库、组件管理
 * 使用方式：import { sendChatMessage, knowledgeQuery, knowledgeExtract } from '@/api'
 */

// 智能问答
export { sendChatMessage } from './modules/chat'

// 知识库：文档管理、检索
export {
  getDocuments,
  uploadDocument,
  deleteDocument,
  knowledgeQuery
} from './modules/knowledge'

// 组件管理：知识抽取、知识图谱更新、自然语言转 Cypher 等
export { knowledgeExtract, extractHealthCheck, storeGraphToNeo4j } from './modules/component'
export { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics } from './modules/component'
export { nl2cypherHealthCheck, nl2cypherGenerate } from './modules/component'
export { intentRecognitionHealthCheck, intentRecognitionRecognize } from './modules/component'
export { answerGenerationHealthCheck, answerGenerationAsk, answerGenerationAskVisualize, answerGenerationQueryGraph } from './modules/component'
export { documentPreprocHealthCheck, documentPreprocConvert } from './modules/component'
