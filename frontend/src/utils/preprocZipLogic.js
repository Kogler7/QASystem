import JSZip from 'jszip'

/**
 * 从 ZIP File 中提取允许的文件（跳过目录、__MACOSX、隐藏文件）。
 *
 * @param {File} zipFile
 * @param {string[]} allowedExts 小写、带点，例如 [".pdf",".docx"]
 * @param {{ maxFiles?: number }} [opts]
 * @returns {Promise<File[]>}
 */
export async function extractAllowedFilesFromZip(zipFile, allowedExts, opts = {}) {
  const maxFiles = Number.isFinite(opts?.maxFiles) ? Math.max(1, opts.maxFiles) : Infinity
  const allow = Array.isArray(allowedExts) ? allowedExts.map((s) => String(s).toLowerCase()) : []

  const arrayBuffer = await zipFile.arrayBuffer()
  const zip = await JSZip.loadAsync(arrayBuffer)
  const entries = Object.values(zip.files)

  const out = []
  for (const entry of entries) {
    if (out.length >= maxFiles) break
    if (!entry || entry.dir) continue
    const entryName = entry.name || ''
    if (!entryName) continue
    if (entryName.startsWith('__MACOSX')) continue
    if (entryName.split('/').some((p) => p.startsWith('.'))) continue

    const lower = entryName.toLowerCase()
    const ext = '.' + (lower.split('.').pop() || '')
    if (allow.length && !allow.includes(ext)) continue
    try {
      const blob = await entry.async('blob')
      const fileObj = new File([blob], entryName, { type: blob.type || '' })
      out.push(fileObj)
    } catch {
      // 单个 entry 失败不影响整体
      continue
    }
  }
  return out
}

/**
 * 如果传入的是单个 .zip，则解压并返回内部允许文件；否则原样返回。
 *
 * @param {File[]} files
 * @param {string[]} allowedExts
 * @param {{ maxFiles?: number }} [opts]
 * @returns {Promise<{ isZip: boolean, files: File[] }>}
 */
export async function expandZipIfNeeded(files, allowedExts, opts = {}) {
  const chosen = Array.isArray(files) ? files : []
  const firstName = String(chosen?.[0]?.name || '').toLowerCase()
  const isZipUpload = chosen.length === 1 && firstName.endsWith('.zip')
  if (!isZipUpload) return { isZip: false, files: chosen }
  const expanded = await extractAllowedFilesFromZip(chosen[0], allowedExts, opts)
  return { isZip: true, files: expanded }
}

