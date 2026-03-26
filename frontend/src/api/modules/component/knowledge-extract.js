/**
 * 组件管理 - 知识抽取 API
 * 请求主站后端 /api/component/knowledge-extract
 */
import { request } from '../../request'

/** 健康检查：GET /api/component/knowledge-extract/health */
export function extractHealthCheck() {
  return request.get('/component/knowledge-extract/health', { timeout: 5000 })
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
