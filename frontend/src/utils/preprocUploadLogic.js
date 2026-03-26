/**
 * 复用“数据预处理”组件与“知识沉淀”流程的上传/模型选择基础逻辑（不涉及 UI）。
 */
 
/**
 * @param {string} name
 * @returns {string} like ".pdf" / ".docx" / "" (lowercase)
 */
export function getLowerExtFromName(name) {
  const n = String(name || '')
  const parts = n.split('.')
  if (parts.length <= 1) return ''
  const ext = parts.pop()
  return ext ? `.${String(ext).toLowerCase()}` : ''
}

/**
 * @param {File|{name?: string}|null|undefined} file
 * @returns {string}
 */
export function getLowerExt(file) {
  return getLowerExtFromName(file?.name)
}

/**
 * 兼容 element-plus el-upload 的 change 事件入参：
 * - (uploadFile, uploadFiles) 其中 uploadFiles[i].raw 才是真 File
 * - 或直接是 input[type=file] 的 FileList / File[]
 *
 * @param {any} uploadFiles
 * @returns {File[]}
 */
export function normalizeToRawFiles(uploadFiles) {
  const files = uploadFiles
  if (!files) return []
  const arr = Array.isArray(files) ? files : Array.from(files)
  if (!arr.length) return []
  // el-upload: [{ raw: File, ... }]
  if (arr[0] && arr[0].raw instanceof File) {
    return arr.map((f) => f.raw).filter(Boolean)
  }
  // input[type=file]: [File, File, ...]
  return arr.filter((f) => f instanceof File)
}

/**
 * 按文件后缀选择默认模型（与后端 getAvailableModels 返回结构对齐）。
 * @param {File} file
 * @param {Record<string, any>} availableModels
 * @param {string} [fallback]
 * @returns {string|null}
 */
export function pickDefaultModelForFile(file, availableModels, fallback = '') {
  const ext = getLowerExt(file)
  const m = availableModels && typeof availableModels === 'object' ? availableModels : {}
  const modelName = m?.[ext]?.default
  if (typeof modelName === 'string' && modelName) return modelName
  if (typeof fallback === 'string' && fallback) return fallback
  return null
}

