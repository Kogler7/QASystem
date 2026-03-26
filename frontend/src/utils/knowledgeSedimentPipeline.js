/**
 * 知识沉淀流水线：与界面解耦，离页后异步仍会继续并同步写入 sessionStorage，
 * 避免「后端跑完但界面卡在中间」等状态错乱。
 */
import {
  uploadDocument,
  documentPreprocConvert,
  parseChunkedJsonExtract,
  nl2cypherSplitMarkdown,
  getAvailableModels,
  processSchemaOutput,
  addFromComputed
} from '@/api'

export const SEDIMENT_STORAGE_KEY = 'qasystem-knowledge-sediment-v1'
export const SEDIMENT_STORAGE_VERSION = 1

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

function mergePublish(prev, partial) {
  return {
    v: SEDIMENT_STORAGE_VERSION,
    ...prev,
    ...partial
  }
}

/**
 * @param {object} opts
 * @param {File} opts.file
 * @param {object} opts.availableModels
 * @param {boolean} opts.modelsLoaded
 * @param {number} opts.kgConfidenceThreshold
 * @param {(snap: object) => void} [opts.onUpdate] 每次落库后回调，用于同步 Vue 视图
 */
export async function runKnowledgeSedimentPipeline({
  file,
  availableModels: initialModels,
  modelsLoaded: initialLoaded,
  kgConfidenceThreshold,
  onUpdate
}) {
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
    availableModels: initialModels && typeof initialModels === 'object' ? initialModels : {},
    modelsLoaded: Boolean(initialLoaded)
  })

  let availableModels = initialModels && typeof initialModels === 'object' ? { ...initialModels } : {}
  let modelsLoaded = Boolean(initialLoaded)

  try {
    if (!modelsLoaded) {
      try {
        const { data } = await getAvailableModels()
        availableModels = data || {}
      } catch {
        availableModels = {}
      }
      modelsLoaded = true
      publish({ availableModels, modelsLoaded })
    }

    const ext = '.' + (file?.name || '').split('.').pop().toLowerCase()
    const modelName = availableModels?.[ext]?.default || null

    const { data: preproc } = await documentPreprocConvert(file, modelName)
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

    const { data: splitRes } = await nl2cypherSplitMarkdown(convertedText)
    let chunks = splitRes?.chunks ?? splitRes?.items ?? splitRes?.results ?? []
    if (!Array.isArray(chunks) || chunks.length === 0) {
      chunks = convertedText.split(/\n{2,}/).filter(Boolean).map((t) => t.trim())
    }
    publish({ chunks, stepIndex: 2 })

    const chunkPayload = JSON.stringify({ chunks }, null, 0)
    const chunkBlob = new Blob([chunkPayload], { type: 'application/json;charset=utf-8' })
    const extractForm = new FormData()
    extractForm.append('file', chunkBlob, 'chunks.json')
    extractForm.append('main_object', 'Term')
    extractForm.append('use_templates', 'true')
    const { data: extract } = await parseChunkedJsonExtract(extractForm)
    publish({ extractResult: extract, stepIndex: 3 })

    if (!extract?.graph) throw new Error('抽取结果缺少 graph，无法更新图谱')

    const { data: kgProcessRes } = await processSchemaOutput(extract, kgConfidenceThreshold)
    if (!kgProcessRes?.success) throw new Error(kgProcessRes?.message || 'KG 置信度计算失败')
    publish({ kgComputeResult: kgProcessRes })

    const { data: kgAddRes } = await addFromComputed(
      kgProcessRes.relations_high,
      kgProcessRes.full_relations,
      kgProcessRes.predictions,
      kgConfidenceThreshold
    )
    publish({ graphUpdateResult: kgAddRes ?? { status: 'ok' }, stepIndex: 4 })

    publish({ uploadLoading: true })
    let stored
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await uploadDocument(formData)
      stored = res?.data
      publish({ storedDocument: stored, uploadLoading: false })
    } catch (uploadErr) {
      publish({ uploadLoading: false })
      throw uploadErr
    }
    return { ok: true, storedDocument: stored }
  } catch (err) {
    const msg = err?.response?.data?.detail || err?.message || '执行失败'
    publish({ stepError: msg, uploadLoading: false })
    throw err
  } finally {
    const prev = readSedimentStorage() || {}
    const snap = mergePublish(prev, { pipelineRunning: false, uploadLoading: false })
    if (file?.name) snap.fileHint = fileToHint(file)
    writeSedimentStorage(snap)
    onUpdate?.(snap)
  }
}
