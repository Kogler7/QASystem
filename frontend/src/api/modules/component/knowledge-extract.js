/**
 * 组件管理 - 知识抽取 API
 * 请求主站后端 /api/component/knowledge-extract
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/knowledge-extract/health */
export function extractHealthCheck() {
  return request.get('/component/knowledge-extract/health', { timeout: 5000 })
}

/** 读取后端 UML 目录解析出的主对象列表 */
export function getUmlMainObjects() {
  return request.get('/component/knowledge-extract/uml-main-objects', { timeout: 10000 })
}

/**
 * 知识抽取
 * @param {object} params - main_object, text, use_templates, llm_base_url, llm_model, llm_api_key
 */
export function knowledgeExtract(params) {
  return request.post('/component/knowledge-extract/extract', {
    main_object: params.main_object.trim(),
    text: params.text.trim(),
    use_templates: params.use_templates ?? true,
    llm_base_url: params.llm_base_url || undefined,
    llm_model: params.llm_model || undefined,
    llm_api_key: params.llm_api_key || undefined
  }, { timeout: 90000 })
}

/**
 * 将抽取结果图结构存储到 Neo4j
 * @param {object} params - graph, neo4j_uri, neo4j_user, neo4j_password
 */
export function storeGraphToNeo4j(params) {
  return request.post('/component/knowledge-extract/store-graph', {
    graph: params.graph,
    neo4j_uri: params.neo4j_uri || 'bolt://localhost:7687',
    neo4j_user: params.neo4j_user || 'neo4j',
    neo4j_password: params.neo4j_password || 'neo4j2025'
  }, { timeout: 15000 })
}

/**
 * 上传切块 JSON（multipart），与 /extract 返回相同结构 { raw, graph }
 * @param {FormData} formData - file, main_object, use_templates, llm_base_url?, llm_model?, llm_api_key?
 */
export function parseChunkedJsonExtract(formData, requestConfig = {}) {
  return request.post('/component/knowledge-extract/parse-chunked-json', formData, {
    timeout: 600000,
    transformRequest: [(data, headers) => {
      if (data instanceof FormData) {
        delete headers['Content-Type']
      }
      return data
    }],
    ...requestConfig
  })
}

/**
 * 上传 UML（.puml）并新增主对象；服务端会保存文件、解析并自动写入 Neo4j
 * @param {FormData} formData - file, new_main_object
 */
export function uploadUmlAndParse(formData, requestConfig = {}) {
  return request.post('/component/knowledge-extract/upload-uml', formData, {
    timeout: 120000,
    transformRequest: [(data, headers) => {
      if (data instanceof FormData) {
        delete headers['Content-Type']
      }
      return data
    }],
    ...requestConfig
  })
}

/**
 * 初始化知识空间
 * @param {object} params - dimension_scales: {dim1: [scale1, scale2], dim2: [scale3]}
 */
export function initKnowledgeSpace(params) {
  return request.post('/component/knowledge-extract/init-knowledge-space', {
    dimension_scales: params.dimension_scales || {}
  }, { timeout: 15000 })
}

/**
 * 根据维度获取刻度列表
 * @param {string} dimension - 维度名称
 */
export function getDimensionScales(dimension) {
  return request.post('/component/knowledge-extract/dimension-scales', {
    dimension: dimension
  }, { timeout: 15000 })
}

/**
 * 知识抽取 V2（本体驱动，无模板匹配）
 * @param {object} params - ontology, text, base_url?, api_key?, model?
 */
export function knowledgeExtractV2(params) {
  return request.post('/component/knowledge-extract/extract-v2', {
    ontology: params.ontology || '',
    text: params.text || '',
    base_url: params.base_url || undefined,
    api_key: params.api_key || undefined,
    model: params.model || undefined
  }, { timeout: 180000 })
}

/**
 * 将知识抽取 V2 结果存入 Neo4j（按 uid 去重）
 * @param {object} graph - 抽取结果的 graph 字段
 */
export function storeExtractionToNeo4j(graph) {
  return request.post('/component/knowledge-extract/store-extraction', {
    graph: graph || { nodes: [], relationships: [] }
  }, { timeout: 15000 })
}
