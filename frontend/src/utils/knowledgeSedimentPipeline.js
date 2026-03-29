/**
 * 知识沉淀流水线：与界面解耦，离页后异步仍会继续并同步写入 sessionStorage，
 * 避免「后端跑完但界面卡在中间」等状态错乱。
 */
import {
  uploadDocument,
  checkDocumentNameExists,
  documentPreprocConvert,
  parseChunkedJsonExtract,
  nl2cypherSplitMarkdown,
  getAvailableModels,
  processSchemaOutput,
  addFromComputed
} from '@/api'
import { PREPROC_EXTENSIONS } from '@/constants/preprocFormats'
import { pickDefaultModelForFile } from '@/utils/preprocUploadLogic'
import { expandZipIfNeeded } from '@/utils/preprocZipLogic'

export const SEDIMENT_STORAGE_KEY = 'qasystem-knowledge-sediment-v1'
export const SEDIMENT_STORAGE_VERSION = 1
let _activeRunController = null
const EXTRACT_TYPES = ['Term', 'RuleType', 'SystemElement']

export class SedimentRunCancelledError extends Error {
  constructor(message = '知识沉淀流程已取消') {
    super(message)
    this.name = 'SedimentRunCancelledError'
  }
}

export function fileToHint(file) {
  if (!file?.name) return null
  return { name: file.name, size: file.size, lastModified: file.lastModified }
}

export function readSedimentStorage() {
  try {
    const raw = sessionStorage.getItem(SEDIMENT_STORAGE_KEY)
    if (!raw) return null
    const s = JSON.parse(raw)
    return s && s.v === SEDIMENT_STORAGE_VERSION ? s : null
  } catch {
    return null
  }
}

export function writeSedimentStorage(snap) {
  try {
    const hasData =
      Boolean(snap?.convertedText?.trim?.()) ||
      (typeof snap?.stepIndex === 'number' && snap.stepIndex > 0) ||
      snap?.extractResult != null ||
      snap?.graphUpdateResult != null ||
      snap?.storedDocument != null ||
      (Array.isArray(snap?.chunks) && snap.chunks.length > 0) ||
      Boolean(snap?.fileHint?.name)
    if (!hasData && !snap?.pipelineRunning) {
      sessionStorage.removeItem(SEDIMENT_STORAGE_KEY)
      return
    }
    sessionStorage.setItem(SEDIMENT_STORAGE_KEY, JSON.stringify(snap))
  } catch (e) {
    console.warn('知识沉淀状态写入失败', e)
  }
}

export function cancelCurrentSedimentRun() {
  if (_activeRunController) {
    _activeRunController.abort()
    _activeRunController = null
  }
  const prev = readSedimentStorage()
  if (!prev) return
  writeSedimentStorage({
    ...prev,
    v: SEDIMENT_STORAGE_VERSION,
    cancelRequested: true,
    pipelineRunning: false,
    uploadLoading: false
  })
}

function normalizeExtractTypes(extractTypes) {
  const types = Array.isArray(extractTypes) ? extractTypes : []
  const normalized = types
    .map((t) => String(t || '').trim())
    .filter((t) => EXTRACT_TYPES.includes(t))
  return normalized.length ? Array.from(new Set(normalized)) : ['Term']
}

function mergeGraphResults(results) {
  const merged = {
    nodes: [],
    relationships: [],
    ontology_relations: []
  }
  const nodeSeen = new Set()
  const relSeen = new Set()
  const ontoSeen = new Set()

  for (const item of results) {
    const graph = item?.graph || {}
    for (const n of graph.nodes || []) {
      const key = JSON.stringify(n)
      if (!nodeSeen.has(key)) {
        nodeSeen.add(key)
        merged.nodes.push(n)
      }
    }
    for (const r of graph.relationships || []) {
      const key = JSON.stringify(r)
      if (!relSeen.has(key)) {
        relSeen.add(key)
        merged.relationships.push(r)
      }
    }
    for (const o of graph.ontology_relations || []) {
      const key = JSON.stringify(o)
      if (!ontoSeen.has(key)) {
        ontoSeen.add(key)
        merged.ontology_relations.push(o)
      }
    }
  }
  return merged
}

function mergePublish(prev, partial) {
  return {
    v: SEDIMENT_STORAGE_VERSION,
    ...prev,
    ...partial
  }
}

/**
 * 为知识沉淀准备输入文件：
 * - 若传入单个 ZIP：自动展开为可预处理扩展名文件
 * - 其余情况：返回过滤后的 File 列表
 *
 * @param {File|File[]|null|undefined} input
 * @returns {Promise<File[]>}
 */
export async function resolveSedimentInputFiles(input) {
  const files = Array.isArray(input) ? input : input ? [input] : []
  if (!files.length) return []

  const expanded = await expandZipIfNeeded(files, PREPROC_EXTENSIONS)
  const candidates = expanded.files || []
  return candidates.filter((f) => PREPROC_EXTENSIONS.includes('.' + (f?.name || '').split('.').pop().toLowerCase()))
}

/**
 * @param {object} opts
 * @param {File} opts.file
 * @param {object} opts.availableModels
 * @param {boolean} opts.modelsLoaded
 * @param {number} opts.kgConfidenceThreshold
 * @param {string[]} [opts.extractTypes]
 * @param {(snap: object) => void} [opts.onUpdate] 每次落库后回调，用于同步 Vue 视图
 */
export async function runKnowledgeSedimentPipeline({
  file,
  availableModels: initialModels,
  modelsLoaded: initialLoaded,
  kgConfidenceThreshold,
  extractTypes,
  onUpdate
}) {
  const runId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const controller = new AbortController()
  _activeRunController = controller

  const ensureActive = () => {
    const s = readSedimentStorage()
    if (!s) return
    // 当前 run 被替换或被显式取消时，立即停止后续步骤。
    if ((s.runId && s.runId !== runId) || s.cancelRequested) {
      throw new SedimentRunCancelledError()
    }
  }

  const publish = (partial) => {
    const prev = readSedimentStorage() || {}
    const snap = mergePublish(prev, partial)
    if (file?.name) snap.fileHint = fileToHint(file)
    writeSedimentStorage(snap)
    onUpdate?.(snap)
  }

  publish({
    pipelineRunning: true,
    stepError: '',
    stepIndex: 0,
    convertedText: '',
    chunks: [],
    extractResult: null,
    graphUpdateResult: null,
    kgComputeResult: null,
    storedDocument: null,
    uploadLoading: false,
    runId,
    cancelRequested: false,
    availableModels: initialModels && typeof initialModels === 'object' ? initialModels : {},
    modelsLoaded: Boolean(initialLoaded)
  })

  let availableModels = initialModels && typeof initialModels === 'object' ? { ...initialModels } : {}
  let modelsLoaded = Boolean(initialLoaded)

  try {
    ensureActive()
    if (!modelsLoaded) {
      try {
        const { data } = await getAvailableModels({ signal: controller.signal })
        availableModels = data || {}
      } catch {
        availableModels = {}
      }
      modelsLoaded = true
      publish({ availableModels, modelsLoaded })
    }
    ensureActive()

    // 仅查重、不入库：避免跑完全流程前写入文档表；重复则立即结束，不跑预处理
    const { data: existsRes } = await checkDocumentNameExists(file.name, { signal: controller.signal })
    ensureActive()
    if (existsRes?.exists) {
      throw new Error(`文件 '${file.name}' 已存在`)
    }

    const modelName = pickDefaultModelForFile(file, availableModels)

    const { data: preproc } = await documentPreprocConvert(file, modelName, { signal: controller.signal })
    ensureActive()
    const convertedText = preproc?.text ?? preproc?.markdown ?? preproc?.content ?? ''
    if (!convertedText?.trim()) throw new Error('预处理未返回有效文本')

    const { content, text, markdown, ...rest } = preproc || {}
    const preprocMeta = {
      ...rest,
      content_length: typeof content === 'string' ? content.length : undefined,
    }

    publish({
      convertedText,
      preprocMeta,
      stepIndex: 1,
      chunks: [],
      extractResult: null,
      graphUpdateResult: null,
      kgComputeResult: null,
      storedDocument: null
    })

    const { data: splitRes } = await nl2cypherSplitMarkdown(convertedText, { signal: controller.signal })
    ensureActive()
    let chunks = splitRes?.chunks ?? splitRes?.items ?? splitRes?.results ?? []
    if (!Array.isArray(chunks) || chunks.length === 0) {
      chunks = convertedText.split(/\n{2,}/).filter(Boolean).map((t) => t.trim())
    }
    publish({ chunks, stepIndex: 2 })

    const selectedTypes = normalizeExtractTypes(extractTypes)
    const chunkPayload = JSON.stringify({ chunks }, null, 0)
    const extracts = []
    for (const mainObject of selectedTypes) {
      const chunkBlob = new Blob([chunkPayload], { type: 'application/json;charset=utf-8' })
      const extractForm = new FormData()
      extractForm.append('file', chunkBlob, 'chunks.json')
      extractForm.append('main_object', mainObject)
      extractForm.append('use_templates', 'true')
      const { data } = await parseChunkedJsonExtract(extractForm, { signal: controller.signal })
      extracts.push({
        type: mainObject,
        raw: data?.raw ?? {},
        graph: data?.graph ?? { nodes: [], relationships: [], ontology_relations: [] }
      })
      ensureActive()
    }
    const extract = {
      raw: selectedTypes.length === 1 ? (extracts[0]?.raw ?? {}) : {},
      graph: mergeGraphResults(extracts),
      selected_types: selectedTypes,
      result_by_type: extracts
    }
    ensureActive()
    publish({ extractResult: extract, stepIndex: 3 })

    if (!extract?.graph) throw new Error('抽取结果缺少 graph，无法更新图谱')

    const { data: kgProcessRes } = await processSchemaOutput(extract, kgConfidenceThreshold, { signal: controller.signal })
    ensureActive()
    if (!kgProcessRes?.success) throw new Error(kgProcessRes?.message || 'KG 置信度计算失败')
    publish({ kgComputeResult: kgProcessRes })

    const { data: kgAddRes } = await addFromComputed(
      kgProcessRes.relations_high,
      kgProcessRes.full_relations,
      kgProcessRes.predictions,
      kgConfidenceThreshold,
      { signal: controller.signal }
    )
    ensureActive()
    publish({ graphUpdateResult: kgAddRes ?? { status: 'ok' }, stepIndex: 4 })

    // 全流程成功后再入库；上传前再查一次，避免流程期间他处已占用同名
    publish({ uploadLoading: true })
    let stored
    try {
      const { data: existsLate } = await checkDocumentNameExists(file.name, { signal: controller.signal })
      ensureActive()
      if (existsLate?.exists) {
        throw new Error(
          `文件 '${file.name}' 已存在（可能在流程进行期间已被其他操作入库）`
        )
      }
      const formData = new FormData()
      formData.append('file', file)
      const res = await uploadDocument(formData, { signal: controller.signal })
      ensureActive()
      stored = res?.data
      publish({ storedDocument: stored, uploadLoading: false })
    } catch (uploadErr) {
      publish({ uploadLoading: false })
      throw uploadErr
    }
    return { ok: true, storedDocument: stored }
  } catch (err) {
    if (err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED' || err?.message === 'canceled') {
      return { ok: false, cancelled: true }
    }
    if (err instanceof SedimentRunCancelledError) {
      return { ok: false, cancelled: true }
    }
    const msg = err?.response?.data?.detail || err?.message || '执行失败'
    publish({ stepError: msg, uploadLoading: false })
    throw err
  } finally {
    if (_activeRunController === controller) {
      _activeRunController = null
    }
    const prev = readSedimentStorage() || {}
    const snap = mergePublish(prev, {
      pipelineRunning: false,
      uploadLoading: false,
      // 只有当前 run 自己清理 cancel 标记，避免误清新 run 的状态
      cancelRequested: prev.runId === runId ? false : prev.cancelRequested
    })
    if (file?.name) snap.fileHint = fileToHint(file)
    writeSedimentStorage(snap)
    onUpdate?.(snap)
  }
}

/**
 * 批量执行知识沉淀流程（顺序执行，避免图谱写入和前端状态互相覆盖）。
 * 不改变单文件入口 runKnowledgeSedimentPipeline 的行为；用于后续批量入口复用。
 *
 * @param {object} opts
 * @param {File[]|File} opts.files
 * @param {object} opts.availableModels
 * @param {boolean} opts.modelsLoaded
 * @param {number} opts.kgConfidenceThreshold
 * @param {(payload: { index: number, total: number, file: File }) => void} [opts.onItemStart]
 * @param {(payload: { index: number, total: number, file: File, result?: any, error?: any }) => void} [opts.onItemFinish]
 * @param {(snap: object) => void} [opts.onUpdate]
 * @returns {Promise<{ total: number, success: number, failed: number, cancelled?: boolean, items: Array<{ fileHint: any, ok: boolean, result?: any, error?: string }> }>}
 */
export async function runKnowledgeSedimentPipelineBatch({
  files,
  availableModels,
  modelsLoaded,
  kgConfidenceThreshold,
  extractTypes,
  onItemStart,
  onItemFinish,
  onUpdate
}) {
  const list = await resolveSedimentInputFiles(files)
  const total = list.length
  const items = []
  if (!total) {
    return { total: 0, success: 0, failed: 0, items }
  }

  let success = 0
  let failed = 0
  for (let i = 0; i < total; i += 1) {
    const file = list[i]
    onItemStart?.({ index: i, total, file })
    try {
      const result = await runKnowledgeSedimentPipeline({
        file,
        availableModels,
        modelsLoaded,
        kgConfidenceThreshold,
        extractTypes,
        onUpdate
      })
      if (result?.cancelled) {
        return { total, success, failed, cancelled: true, items }
      }
      items.push({ fileHint: fileToHint(file), ok: true, result })
      success += 1
      onItemFinish?.({ index: i, total, file, result })
    } catch (error) {
      if (error instanceof SedimentRunCancelledError) {
        return { total, success, failed, cancelled: true, items }
      }
      const msg = error?.response?.data?.detail || error?.message || '执行失败'
      items.push({ fileHint: fileToHint(file), ok: false, error: msg })
      failed += 1
      onItemFinish?.({ index: i, total, file, error })
    }
  }
  return { total, success, failed, items }
}
