/**
 * 组件管理 - API 统一入口
 * 新增组件时：在此导出对应模块的接口
 */
export { knowledgeExtract, extractHealthCheck } from './knowledge-extract'
export { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics } from './kg-update'
export { nl2cypherHealthCheck, nl2cypherGenerate } from './nl2cypher'
export { intentRecognitionHealthCheck, intentRecognitionRecognize } from './intent-recognition'
export { answerGenerationHealthCheck, answerGenerationAsk, answerGenerationAskVisualize } from './answer-generation'
