<script setup>
import { ref, onMounted, onBeforeUnmount, computed, watch, nextTick } from 'vue'
import { Upload, Document, Download } from '@element-plus/icons-vue'
import { documentPreprocHealthCheck, documentPreprocConvert, documentPreprocConvertToPdf, getAvailableModels } from '@/api'
import { ElMessage } from 'element-plus'
import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import * as mammoth from 'mammoth/mammoth.browser'
import * as XLSX from 'xlsx'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { getLowerExtFromName, normalizeToRawFiles, pickDefaultModelForFile } from '@/utils/preprocUploadLogic'
import { expandZipIfNeeded } from '@/utils/preprocZipLogic'

// 设置PDF.js worker
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

marked.use(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      const validLang = lang && hljs.getLanguage(lang) ? lang : 'plaintext'
      return hljs.highlight(code, { language: validLang }).value
    }
  })
)

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')
const fileList = ref([])
const batchFiles = ref([])
const fileInput = ref(null)
const batchFileInput = ref(null)
const batchConcurrency = ref(4) // 并发数（前端控制，建议 3-5）
const batchLoading = ref(false)
const previewAreaRef = ref(null)
const pdfContainerRef = ref(null)
const result = ref(null)
const availableModels = ref({})
const selectedDataType = ref('')
const selectedModel = ref('')
const detectedFirstFileExt = ref('')
const previewContent = ref('')
const previewType = ref('') // 'image', 'pdf', 'text', 'docx', 'excel', 'doc', 'html', 'other'
const wordHint = ref('')
const excelSheets = ref([])
const activeExcelSheet = ref(0)
const currentPdfFile = ref(null)

const acceptTypes = '.pdf,.docx,.doc,.xlsx,.xls,.csv,.html,.htm,.txt,.jpg,.jpeg,.png,.gif,.bmp,.zip'
const viewMode = ref('rendered') // 'rendered' or 'source'

const dataTypes = computed(() => Object.keys(availableModels.value))

const modelsForSelectedType = computed(() => {
  if (!selectedDataType.value || !availableModels.value[selectedDataType.value]) return []
  return Object.entries(availableModels.value[selectedDataType.value].models).map(([key, value]) => ({
    value: key,
    label: value.name,
    description: value.description,
    pros: value.pros,
    cons: value.cons,
    scenario: value.scenario
  }))
})

const defaultModelForType = computed(() => {
  if (!selectedDataType.value || !availableModels.value[selectedDataType.value]) return ''
  return availableModels.value[selectedDataType.value].default
})

const currentModelInfo = computed(() => {
  if (!selectedDataType.value || !selectedModel.value || !availableModels.value[selectedDataType.value]) return null
  return availableModels.value[selectedDataType.value].models[selectedModel.value]
})

const canDownloadMarkdown = computed(() => {
  if (!result.value?.success) return false
  const content = contentDisplay()
  return Boolean(content && content.trim())
})

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await documentPreprocHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '文档预处理模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端并安装 algorithm/preproc 依赖'
  }
}

const loadAvailableModels = async () => {
  try {
    const { data } = await getAvailableModels()
    availableModels.value = data
  } catch (err) {
    ElMessage.error('获取可用模型失败')
    console.error(err)
  }
}

const resetPreviewState = () => {
  previewContent.value = ''
  previewType.value = ''
  wordHint.value = ''
  excelSheets.value = []
  activeExcelSheet.value = 0
  currentPdfFile.value = null
  if (pdfContainerRef.value) {
    pdfContainerRef.value.innerHTML = ''
  }
}

const handleChange = async (_uploadFile, uploadFiles) => {
  // 兼容 el-upload 和 input[type=file] 两种调用方式
  const raws = normalizeToRawFiles(uploadFiles)
  fileList.value = raws.length ? [raws[0]] : []
  // 自动匹配数据类型
  if (fileList.value.length > 0) {
    const file = fileList.value[0]
    const ext = getLowerExtFromName(file.name)
    if (dataTypes.value.includes(ext)) {
      selectedDataType.value = ext
    }
    await generatePreview(file)
  } else {
    resetPreviewState()
  }
}

const handleBatchChange = async (_uploadFile, uploadFiles) => {
  // 支持 input[type=file] 或 el-upload 多文件返回
  const chosen = normalizeToRawFiles(uploadFiles)

  // 建议最多 10 个，超出提示
  if (chosen.length > 10) {
    ElMessage.warning('建议一次最多选择 10 个文件，已选中过多文件可能导致请求拥堵')
  }

  let filesToAppend = chosen
  // 只接受的扩展
  const allowedExts = ['.pdf', '.docx', '.doc', '.xlsx', '.xls', '.csv', '.html', '.htm']
  try {
    const expanded = await expandZipIfNeeded(chosen, allowedExts)
    if (expanded.isZip) {
      if (!expanded.files.length) {
        ElMessage.warning('ZIP 包内未找到可用文件')
        return
      }
      ElMessage.success(`ZIP 解压完成：已添加 ${expanded.files.length} 个文件到批量队列`)
    }
    filesToAppend = expanded.files
  } catch (e) {
    console.error('ZIP 解压失败:', e)
    ElMessage.error('ZIP 文件解压失败，请确认文件是否损坏')
    return
  }

  // 不清空已有列表，追加到现有 batchFiles
  const baseIndex = batchFiles.value.length
  const newItems = filesToAppend.map((f, i) => ({
    id: `${Date.now()}-${baseIndex + i}`,
    file: f,
    name: f.name,
    status: 'pending',
    result: null,
    error: null
  }))
  batchFiles.value = batchFiles.value.concat(newItems)
  // 如果当前列表之前为空，则识别第一个文件类型并在数据类型处显示
  if (baseIndex === 0 && newItems.length > 0) {
    const detectedExt = '.' + (newItems[0].name || '').split('.').pop().toLowerCase()
    if (dataTypes.value.includes(detectedExt)) {
      selectedDataType.value = detectedExt
      detectedFirstFileExt.value = ''
    } else {
      detectedFirstFileExt.value = detectedExt
    }
  }
  // 不自动开始，等待用户点击“开始批量解析”
}

// 删除误传的文件项
const deleteBatchItem = (item) => {
  if (!item) return
  batchFiles.value = batchFiles.value.filter((it) => it.id !== item.id)
  // 如果删除影响当前展示，调整 selected index
  if (selectedBatchIndex.value >= batchFiles.value.length) {
    selectedBatchIndex.value = Math.max(0, batchFiles.value.length - 1)
  }
}

// 处理单个文件项（复用后端单文件接口）
const processBatchItem = async (item) => {
  item.status = 'processing'
  item.error = null
  try {
    const modelName = pickDefaultModelForFile(item.file, availableModels.value, '')
    const { data } = await documentPreprocConvert(item.file, modelName || '')
    item.result = data
    if (data?.success) {
      item.status = 'success'
    } else {
      item.status = 'failed'
      item.error = data?.error || '解析失败'
    }
  } catch (err) {
    item.status = 'failed'
    item.error = err?.response?.data?.detail || err?.message || String(err)
  }
}

// 并发批量处理（前端控制并发数）
const startBatchProcessing = async () => {
  if (!batchFiles.value.length) {
    ElMessage.warning('当前无批量文件可处理')
    return
  }
  if (batchLoading.value) return
  batchLoading.value = true
  const concurrency = Math.max(1, Math.min(5, Number(batchConcurrency.value) || 3))
  let index = 0
  let active = 0

  return new Promise((resolve) => {
    const next = () => {
      while (active < concurrency && index < batchFiles.value.length) {
        const item = batchFiles.value[index++]
        // 仅处理待处理或重试标记的项
        if (!item || item.status === 'processing' || item.status === 'success') continue
        active += 1
        processBatchItem(item).finally(() => {
          active -= 1
          // 每当有一个完成，触发下一个
          if (index >= batchFiles.value.length && active === 0) {
            batchLoading.value = false
            ElMessage.success('批量处理完成')
            resolve()
          } else {
            next()
          }
        })
      }
      // 如果没有任务且无活动，则结束
      if (index >= batchFiles.value.length && active === 0) {
        batchLoading.value = false
        resolve()
      }
    }
    next()
  })
}

const retryBatchItem = async (item) => {
  if (!item) return
  if (item.status !== 'failed') return
  item.status = 'pending'
  item.error = null
  // 启动一次短期的并发处理以包含此项
  // 若正在批量处理中，会在队列中自然处理
  if (!batchLoading.value) {
    startBatchProcessing()
  }
}

const viewBatchResult = (item) => {
  if (!item) return
  const idx = batchFiles.value.findIndex((it) => it.id === item.id)
  if (idx === -1) return
  selectedBatchIndex.value = idx
  // 展示原文件预览
  fileList.value = [item.file]
  generatePreview(item.file)
  if (!item.result) {
    ElMessage.info('该文件暂无解析结果')
    result.value = null
    return
  }
  // 将该文件的解析结果展示到右侧预览区（覆盖当前 result）
  result.value = item.result
}

const renderPdfPreview = async (file) => {
  previewType.value = 'pdf'
  currentPdfFile.value = file
  await nextTick()

  const container = pdfContainerRef.value
  if (!container) return

  try {
    container.innerHTML = ''
    const arrayBuffer = await file.arrayBuffer()
    const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise

    const containerWidth = Math.max((previewAreaRef.value?.clientWidth || container.clientWidth || 600) - 28, 320)
    const devicePixelRatio = window.devicePixelRatio || 1

    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
      const page = await pdf.getPage(pageNumber)
      const baseViewport = page.getViewport({ scale: 1 })
      const scale = containerWidth / baseViewport.width
      const viewport = page.getViewport({ scale })

      const wrapper = document.createElement('div')
      wrapper.className = 'pdf-page-wrapper'

      const canvas = document.createElement('canvas')
      canvas.className = 'pdf-page-canvas'
      canvas.width = Math.floor(viewport.width * devicePixelRatio)
      canvas.height = Math.floor(viewport.height * devicePixelRatio)
      canvas.style.width = `${viewport.width}px`
      canvas.style.height = `${viewport.height}px`

      const context = canvas.getContext('2d')
      context.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0)
      await page.render({ canvasContext: context, viewport }).promise

      wrapper.appendChild(canvas)
      container.appendChild(wrapper)
    }
  } catch (error) {
    console.error('PDF 预览渲染失败:', error)
    previewType.value = 'other'
    previewContent.value = 'PDF 预览失败，请重试或刷新页面后再次选择文件。'
  }
}

const renderDocxPreview = async (file) => {
  previewType.value = 'docx'
  const arrayBuffer = await file.arrayBuffer()
  const options = {
    includeDefaultStyleMap: true,
    preserveEmptyParagraphs: true,
    styleMap: [
      "p[style-name='Heading 1'] => h1:fresh",
      "p[style-name='Heading 2'] => h2:fresh",
      "p[style-name='Heading 3'] => h3:fresh",
      "p[style-name='Heading 4'] => h4:fresh",
      "r[style-name='Strong'] => strong",
      "r[style-name='Emphasis'] => em",
      'table => table.word-table:fresh'
    ]
  }

  const { value, messages } = await mammoth.convertToHtml({ arrayBuffer }, options)
  const hasFormulaNotice = messages?.some((item) => /oMath|equation|公式/i.test(item.message || ''))
  wordHint.value = hasFormulaNotice ? '检测到公式内容，Word 预览可能无法完整还原，建议参考 PDF 版本。' : ''

  const formulaPlaceholder = hasFormulaNotice
    ? '<p class="word-formula-placeholder">【公式占位】部分公式无法在 Word 预览中完整渲染，请优先查看 PDF 预览。</p>'
    : ''

  previewContent.value = `${formulaPlaceholder}${value}`
    .replace(/page-break-before:[^;"']+;?/gi, '')
    .replace(/class="[^"]*page-break[^"]*"/gi, '')
}

const isLikelyBinaryBuffer = (bytes) => {
  if (!bytes?.length) return false
  if (
    (bytes[0] === 0x50 && bytes[1] === 0x4B && bytes[2] === 0x03 && bytes[3] === 0x04) ||
    (bytes[0] === 0xD0 && bytes[1] === 0xCF && bytes[2] === 0x11 && bytes[3] === 0xE0)
  ) {
    return true
  }

  const sampleSize = Math.min(bytes.length, 4096)
  let zeroCount = 0
  for (let i = 0; i < sampleSize; i += 1) {
    if (bytes[i] === 0) zeroCount += 1
  }
  return zeroCount / sampleSize > 0.02
}

const scoreDecodedCsv = (text) => {
  if (!text) return -999
  const controlChars = (text.match(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g) || []).length
  const replacementChars = (text.match(/�/g) || []).length
  const separators = (text.match(/,|;|\t|\|/g) || []).length
  return separators * 2 - controlChars * 6 - replacementChars * 8
}

const decodeCsvText = (arrayBuffer) => {
  const bytes = new Uint8Array(arrayBuffer)

  if (bytes.length >= 2) {
    if (bytes[0] === 0xFF && bytes[1] === 0xFE) {
      return { text: new TextDecoder('utf-16le').decode(bytes), encoding: 'utf-16le' }
    }
    if (bytes[0] === 0xFE && bytes[1] === 0xFF) {
      return { text: new TextDecoder('utf-16be').decode(bytes), encoding: 'utf-16be' }
    }
  }

  if (bytes.length >= 3 && bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) {
    return { text: new TextDecoder('utf-8').decode(bytes), encoding: 'utf-8-bom' }
  }

  const encodings = ['utf-8', 'gb18030', 'gbk', 'big5', 'shift_jis', 'windows-1252']
  let best = { text: '', encoding: 'unknown', score: -999999 }

  for (const encoding of encodings) {
    try {
      const decoded = new TextDecoder(encoding, { fatal: true }).decode(bytes)
      const score = scoreDecodedCsv(decoded)
      if (score > best.score) {
        best = { text: decoded, encoding, score }
      }
    } catch {
      continue
    }
  }

  if (best.text) return { text: best.text, encoding: best.encoding }

  return { text: new TextDecoder('utf-8').decode(bytes), encoding: 'utf-8-fallback' }
}

const parseCsvToSheet = (csvText) => {
  const normalized = csvText?.replace(/^\uFEFF/, '') || ''
  if (!normalized.trim()) {
    return { name: 'Sheet1', html: '<table><tbody><tr><td>空文件</td></tr></tbody></table>' }
  }

  try {
    const workbook = XLSX.read(normalized, { type: 'string', raw: false })
    const firstSheetName = workbook.SheetNames[0]
    const sheet = workbook.Sheets[firstSheetName]
    return {
      name: firstSheetName || 'Sheet1',
      html: XLSX.utils.sheet_to_html(sheet, {
        editable: false,
        id: `sheet-${firstSheetName || 'Sheet1'}`
      })
    }
  } catch {
    const lines = normalized.split(/\r\n|\n|\r/).filter((line) => line.trim().length > 0)
    const rowsHtml = lines.map((line) => `<tr><td>${line}</td></tr>`).join('')
    return { name: 'Sheet1', html: `<table><tbody>${rowsHtml}</tbody></table>` }
  }
}

const renderExcelPreview = async (file) => {
  previewType.value = 'excel'
  const arrayBuffer = await file.arrayBuffer()

  if (/\.csv$/i.test(file.name)) {
    const bytes = new Uint8Array(arrayBuffer)
    if (isLikelyBinaryBuffer(bytes)) {
      const workbook = XLSX.read(arrayBuffer, { type: 'array', cellStyles: true, cellDates: true })
      excelSheets.value = workbook.SheetNames.map((sheetName) => ({
        name: sheetName,
        html: XLSX.utils.sheet_to_html(workbook.Sheets[sheetName], {
          editable: false,
          id: `sheet-${sheetName}`
        })
      }))
      activeExcelSheet.value = 0
      return
    }

    const { text: csvText } = decodeCsvText(arrayBuffer)
    excelSheets.value = [parseCsvToSheet(csvText)]
    activeExcelSheet.value = 0
    return
  }

  const workbook = XLSX.read(arrayBuffer, {
    type: 'array',
    cellStyles: true,
    cellDates: true
  })

  excelSheets.value = workbook.SheetNames.map((sheetName) => ({
    name: sheetName,
    html: XLSX.utils.sheet_to_html(workbook.Sheets[sheetName], {
      editable: false,
      id: `sheet-${sheetName}`
    })
  }))
  activeExcelSheet.value = 0
}

const generatePreview = async (file) => {
  resetPreviewState()
  const fileType = (file.type || '').toLowerCase()
  const fileName = file.name.toLowerCase()

  if (fileType.startsWith('image/') || /\.(jpg|jpeg|png|gif|bmp)$/i.test(fileName)) {
    previewType.value = 'image'
    const reader = new FileReader()
    reader.onload = (e) => {
      previewContent.value = e.target.result
    }
    reader.readAsDataURL(file)
  } else if (/\.pdf$/i.test(fileName) || fileType === 'application/pdf') {
    await renderPdfPreview(file)
  } else if (/\.docx$/i.test(fileName)) {
    await renderDocxPreview(file)
  } else if (/\.(xlsx|xls|csv)$/i.test(fileName)) {
    await renderExcelPreview(file)
  } else if (/\.doc$/i.test(fileName)) {
    // 优先尝试将 DOC 转为 PDF 并在前端直接渲染原件（更能保留原始排版）
    previewContent.value = '正在转换 DOC 为 PDF 并生成预览...'
    try {
      const resp = await documentPreprocConvertToPdf(file)
      // axios response with blob in data
      const blob = resp?.data
      if (blob && blob.size) {
        // 直接使用 PDF 渲染函数（renderPdfPreview 支持 Blob，因为 Blob 有 arrayBuffer 方法）
        await renderPdfPreview(blob)
        // 不覆盖 result 中的解析结果（原解析流程仍保留在批量项中）
      } else {
        // 回退到原先的 Markdown 预览（使用解析接口）
        throw new Error('转换为 PDF 未返回有效内容')
      }
    } catch (err) {
      console.warn('DOC 原件 PDF 预览失败，回退到解析预览：', err)
      // 回退：使用 documentPreprocConvert 将 DOC 转为 Markdown/HTML 以便预览（不修改源文件）
      previewType.value = 'doc'
      previewContent.value = '正在解析 DOC（回退模式），可能需要一些时间...'
      try {
        const ext = '.doc'
        let modelName = ''
        if (availableModels.value && availableModels.value[ext]) {
          modelName = availableModels.value[ext].default || ''
        }
        const { data } = await documentPreprocConvert(file, modelName)
        if (data?.success) {
          const content = data.content_preview ?? data.content ?? ''
          if (typeof content === 'string') {
            previewContent.value = marked.parse(content)
          } else {
            previewContent.value = JSON.stringify(content)
          }
          result.value = data
        } else {
          previewContent.value = data?.error || 'DOC 解析失败，无法预览'
        }
      } catch (err2) {
        console.error('DOC 预览解析失败:', err2)
        previewContent.value = 'DOC 预览失败，请稍后重试或转换为 DOCX/PDF 查看。'
      }
    }
  } else if (/\.(html|htm)$/i.test(fileName) || fileType === 'text/html') {
    previewType.value = 'html'
    const reader = new FileReader()
    reader.onload = (e) => {
      previewContent.value = e.target.result || ''
    }
    reader.readAsText(file)
  } else if (fileType.startsWith('text/') || /\.(txt|md)$/i.test(fileName)) {
    previewType.value = 'text'
    const reader = new FileReader()
    reader.onload = (e) => {
      const content = e.target.result
      previewContent.value = content.length > 1000 ? content.substring(0, 1000) + '...' : content
    }
    reader.readAsText(file)
  } else {
    previewType.value = 'other'
    previewContent.value = '暂不支持该文件类型的高保真预览。'
  }
}

const submit = async () => {
  // 单文件解析：对当前选中展示的批量项进行解析
  const idx = selectedBatchIndex.value || 0
  const item = batchFiles.value?.[idx]
  if (!item) {
    ElMessage.warning('请先选择要解析的文件')
    return
  }
  // 如果页面有模型选择优先使用，否则根据文件后缀使用默认模型
  let modelName = ''
  if (selectedModel.value) {
    modelName = selectedModel.value
  } else {
    modelName = pickDefaultModelForFile(item.file, availableModels.value, '') || ''
  }
  // 调用解析并更新该项状态
  item.status = 'processing'
  loading.value = true
  try {
    const { data } = await documentPreprocConvert(item.file, modelName)
    item.result = data
    if (data?.success) {
      item.status = 'success'
      result.value = data
      ElMessage.success('单文件解析完成')
    } else {
      item.status = 'failed'
      item.error = data?.error || '解析未成功'
      ElMessage.warning(item.error)
    }
  } catch (err) {
    item.status = 'failed'
    item.error = err?.response?.data?.detail || err?.message || String(err)
    ElMessage.error(item.error)
  } finally {
    loading.value = false
  }
}

const autoParse = async () => {
  // 保留但不再直接使用，单文件解析已在 submit 中处理
  return
}

const contentDisplay = () => {
  if (!result.value) return ''
  const c = result.value.content_preview ?? result.value.content
  if (c == null) return ''
  return typeof c === 'string' ? c : JSON.stringify(c)
}

// 展示层：清理 LaTeX 表达式用的通用函数（仅影响展示）
const displayCleanMath = (expr) => {
  if (!expr || typeof expr !== 'string') return expr
  let s = expr
  // 移除 LaTeX 中常见的 ~ 非断行空格标记与 unicode 不间断空格
  s = s.replace(/~+/g, '')
  s = s.replace(/\u00A0+/g, '')

  // 将 \pm 转为 ±
  s = s.replace(/\\pm/g, '±')

  // 规范化 \mathrm{...} 内部：移除所有花括号并去掉内部空白（A C -> AC，k V -> kV，2 5 -> 25）
  // 支持像 \mathrm{{km/h}} 这样的双重花括号
  s = s.replace(/\\mathrm\s*\{\s*([^}]*)\s*\}/g, (_, inner) => {
    // 去掉内部可能存在的额外大括号并删除所有空白和不间断空格、~等
    const cleanedInner = (inner || '').replace(/[{}\u00A0~\s]+/g, '')
    return cleanedInner
  })

  // 去掉数字与点之间的多余空格（0 . 8 -> 0.8）
  s = s.replace(/(\d)\s*\.\s*(\d)/g, '$1.$2')

  // 反复合并数字之间的空格（3 7 5 -> 375），循环以处理多位中间有空格的情况
  while (/\d\s+\d/.test(s)) {
    s = s.replace(/(\d)\s+(\d)/g, '$1$2')
  }

  // 合并数字与字母之间的空格（0.8 m -> 0.8m, 25 kV -> 25kV）
  s = s.replace(/(\d)\s+([a-zA-Z\\])/g, '$1$2')
  s = s.replace(/([a-zA-Z\\])\s+(\d)/g, '$1$2')

  // 移除 \; \: 等显示空格命令留下的多余空格（保守处理）
  s = s.replace(/\\[;:\s]+/g, '')

  // 压缩连续空白为单空格（最后保险处理）
  s = s.replace(/\s{2,}/g, ' ')

  return s
}

// 将 LaTeX 表达式转换为纯文本用于展示（移除 $ 包裹）
const latexToPlain = (text) => {
  if (!text || typeof text !== 'string') return text
  return text.replace(/\$\$([\s\S]+?)\$\$|\$([^$]+?)\$/g, (match, block, inline) => {
    try {
      const content = block ?? inline
      // displayCleanMath 保持单位、数字的合并和 \mathrm 内部清理
      const cleaned = displayCleanMath(content)
      // 如果是块级表达式，保留为单独一行文本（不加 $ 或 $$）
      if (block) return `\n${cleaned}\n`
      // 行内表达式直接替换为清理后的文本
      return cleaned
    } catch (e) {
      return match
    }
  })
}

const renderedMarkdown = computed(() => {
  if (!result.value) return ''
  const content = contentDisplay()
  if (!content) return ''
  
  // 使用marked库渲染Markdown
  try {
    // 将Markdown中的 LaTeX 表达式替换为纯文本再交由 marked 渲染
    const plainized = latexToPlain(content)
    return marked.parse(plainized)
  } catch (err) {
    console.warn('Markdown渲染失败:', err)
    return content
  }
})

onMounted(() => {
  checkHealth()
  loadAvailableModels()
  window.addEventListener('resize', handleWindowResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleWindowResize)
})

watch(selectedDataType, (newType) => {
  if (newType && availableModels.value[newType]) {
    selectedModel.value = availableModels.value[newType].default
  } else {
    selectedModel.value = ''
  }
})

// 当批量文件列表发生变化且没有显式选择时，默认选中第一个并展示
watch(batchFiles, (newList) => {
  if (newList && newList.length && (selectedBatchIndex.value == null || selectedBatchIndex.value >= newList.length)) {
    selectedBatchIndex.value = 0
  }
})

const onReplaceFile = () => {
  // 清空批量文件列表并重置预览
  batchFiles.value = []
  selectedBatchIndex.value = 0
  fileList.value = []
  resetPreviewState()
  // 打开选择器以方便替换
  openFilePicker()
}

const downloadMarkdown = () => {
  if (!canDownloadMarkdown.value) {
    ElMessage.warning('暂无可下载的解析结果')
    return
  }

  const markdown = contentDisplay()
  const originName = result.value?.metadata?.file_name || fileList.value?.[0]?.name || 'parsed'
  const stem = originName.includes('.') ? originName.substring(0, originName.lastIndexOf('.')) : originName
  const filename = `${stem}_parsed.md`

  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// 下载指定文件项的解析 Markdown（若存在）
const downloadBatchItem = (item) => {
  if (!item || !item.result) {
    ElMessage.warning('该文件暂无解析结果可下载')
    return
  }
  const data = item.result
  const content = data.content_preview ?? data.content
  if (!content) {
    ElMessage.warning('该文件解析内容为空')
    return
  }
  const markdown = typeof content === 'string' ? content : JSON.stringify(content)
  const originName = data?.metadata?.file_name || item.name || 'parsed'
  const stem = originName.includes('.') ? originName.substring(0, originName.lastIndexOf('.')) : originName
  const filename = `${stem}_parsed.md`
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// 当前展示的批量项索引（用于在未手动查看时默认展示第一个）
const selectedBatchIndex = ref(0)

// 当选中项变化时展示该文件的原始预览与解析结果（若有）
watch(selectedBatchIndex, async (idx) => {
  const item = batchFiles.value?.[idx]
  if (!item) {
    // 无项则清空
    fileList.value = []
    result.value = null
    resetPreviewState()
    return
  }
  // 展示原文件预览
  fileList.value = [item.file]
  await generatePreview(item.file)
  // 若已有解析结果则展示
  if (item.result) {
    result.value = item.result
  } else {
    result.value = null
  }
})

const openFilePicker = () => {
  fileInput.value && fileInput.value.click()
}

// 上传按钮处理：将文件追加到批量列表（不清空已有）
const handleUploadClick = () => {
  openFilePicker()
}

let resizeTimer = null
const handleWindowResize = () => {
  if (previewType.value !== 'pdf' || !currentPdfFile.value) return
  window.clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(() => {
    renderPdfPreview(currentPdfFile.value)
  }, 180)
}



const renderLaTeX = (text) => {
  // 使用展示层清理后渲染 LaTeX
  return text.replace(/\$\$([\s\S]+?)\$\$|\$([^$]+?)\$/g, (match, block, inline) => {
    try {
      if (block) {
        const cleaned = displayCleanMath(block)
        return katex.renderToString(cleaned, { displayMode: true })
      } else if (inline) {
        const cleaned = displayCleanMath(inline)
        return katex.renderToString(cleaned, { displayMode: false })
      }
    } catch (e) {
      console.warn('KaTeX 渲染失败:', match, e)
      return match
    }
    return match
  })
}
</script>

<template>
  <div class="preproc-view">
    <div class="preproc-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="preproc-inner">
      <h1 class="page-title">
        <span class="title-highlight">多源数据解析组件库</span>
      </h1>
      <div class="component-intro">
        <p class="intro-title">组件功能说明</p>
        <p><strong>功能概述：</strong>对多源文档进行统一解析与预览，并输出可用于知识处理的 Markdown/文本结果。</p>
        <p><strong>处理对象类型：</strong>PDF、DOCX、DOC、XLSX、XLS、CSV、HTML等常见文件。</p>
        <p><strong>使用流程：</strong></p>
        <ol class="intro-steps">
          <li>点击“上传文件”或“批量上传”添加文件（批量上传会追加到当前队列）。</li>
          <li>组件会识别第一个已上传文件的后缀并提示数据类型，可在“数据类型”处确认或手动切换解析模型。</li>
          <li>可使用“单文件解析”对当前选中项执行解析，或点击“批量解析”处理队列中所有待处理文件。</li>
          <li>每个文件支持：查看原文件、查看/下载解析后的 Markdown、重试或删除误传文件。</li>
        </ol>
      </div>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">服务状态：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>上传并转换</span>
        </template>

        <!-- 数据类型选择 -->
        <el-form-item label="数据类型" required>
          <el-select v-model="selectedDataType" placeholder="请选择数据类型" style="width: 200px">
            <el-option
              v-for="type in dataTypes"
              :key="type"
              :label="type"
              :value="type"
            />
          </el-select>
          <span v-if="detectedFirstFileExt" class="detected-ext">检测到文件类型：{{ detectedFirstFileExt }}</span>
          <span class="form-tip">根据文件后缀自动匹配，可手动修改</span>
        </el-form-item>

        <!-- 模型选择 -->
        <el-form-item v-if="selectedDataType" label="解析模型" required>
          <el-select v-model="selectedModel" placeholder="请选择解析模型" style="width: 200px">
            <el-option
              v-for="model in modelsForSelectedType"
              :key="model.value"
              :label="model.label"
              :value="model.value"
            />
          </el-select>
        </el-form-item>

        <!-- 模型说明卡片 -->
        <el-card v-if="selectedModel && selectedDataType" class="model-info-card" shadow="never">
          <template #header>
            <span>{{ currentModelInfo?.name }} 模型说明</span>
          </template>
          <div class="model-details">
            <p><strong>描述：</strong>{{ currentModelInfo?.description }}</p>
            <p><strong>适用场景：</strong>{{ currentModelInfo?.scenario }}</p>
            <div class="pros-cons">
              <div class="pros">
                <strong>优点：</strong>
                <ul>
                  <li v-for="pro in currentModelInfo?.pros" :key="pro">{{ pro }}</li>
                </ul>
              </div>
              <div class="cons">
                <strong>缺点：</strong>
                <ul>
                  <li v-for="con in currentModelInfo?.cons" :key="con">{{ con }}</li>
                </ul>
              </div>
            </div>
          </div>
        </el-card>

        <div class="file-layout">
          <!-- 左侧：文件预览 -->
          <div class="file-left">
            <div class="preview-area" v-if="fileList.length" ref="previewAreaRef">
              <template v-if="previewType === 'image'">
                <img :src="previewContent" alt="预览图片" class="preview-image" />
              </template>
              <template v-else-if="previewType === 'pdf'">
                <div ref="pdfContainerRef" class="preview-pdf-pages"></div>
              </template>
              <template v-else-if="previewType === 'docx'">
                <el-alert
                  v-if="wordHint"
                  :title="wordHint"
                  type="warning"
                  show-icon
                  :closable="false"
                  class="preview-word-hint"
                />
                <div class="preview-word" v-html="previewContent"></div>
              </template>
              <template v-else-if="previewType === 'excel'">
                <div class="excel-tabs" v-if="excelSheets.length > 1">
                  <button
                    v-for="(sheet, index) in excelSheets"
                    :key="sheet.name"
                    class="excel-tab"
                    :class="{ active: activeExcelSheet === index }"
                    @click="activeExcelSheet = index"
                  >
                    {{ sheet.name }}
                  </button>
                </div>
                <div
                  v-if="excelSheets.length"
                  class="preview-excel"
                  v-html="excelSheets[activeExcelSheet]?.html"
                ></div>
              </template>
              <template v-else-if="previewType === 'doc'">
                <div class="preview-word" v-html="previewContent"></div>
              </template>
              <template v-else-if="previewType === 'html'">
                <iframe
                  class="preview-html-frame"
                  :srcdoc="previewContent"
                  sandbox="allow-same-origin"
                  referrerpolicy="no-referrer"
                ></iframe>
              </template>
              <template v-else-if="previewType === 'text'">
                <pre class="preview-text">{{ previewContent }}</pre>
              </template>
              <template v-else>
                <p class="preview-tip">{{ previewContent || '无法预览此文件类型' }} <small>({{ previewType || 'unknown' }})</small></p>
              </template>
            </div>
          </div>

          <!-- 右侧：Markdown 渲染 -->
          <div class="file-right">
            <div v-if="renderedMarkdown" class="markdown-preview" v-html="renderedMarkdown"></div>
            <div v-else class="placeholder">解析后的内容将显示在此处</div>
            <!-- <div v-html="renderLaTeX(content)"></div> -->
          </div>
        </div>

        <div class="action-row">
          <el-button
            type="primary"
            size="default"
            :icon="Upload"
            @click="handleUploadClick"
          >
            上传文件
          </el-button>
          <el-button
            type="primary"
            size="default"
            :icon="Upload"
            @click="onReplaceFile"
          >
            更换文件
          </el-button>
          <el-button
            type="primary"
            :loading="loading"
            :icon="Document"
            @click="submit"
          >
            单文件解析
          </el-button>
          <el-button
            type="primary"
            size="default"
            :loading="batchLoading"
            @click="startBatchProcessing"
          >
            批量解析
          </el-button>
          <!-- 下载已移至每个文件行的操作中 -->
          <span class="upload-tip">支持 PDF、DOCX、DOC、XLSX、XLS、CSV、HTML</span>
        </div>
        <input
          ref="fileInput"
          type="file"
          multiple
          :accept="acceptTypes"
          style="display: none"
          @change="handleBatchChange($event.target.files[0], $event.target.files)"
        />
        <div v-if="batchFiles.length" class="batch-list">
          <strong>文件列表：</strong>
          <table class="batch-table">
            <thead>
              <tr>
                <th>文件名</th>
                <th>状态</th>
                    <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in batchFiles" :key="item.id">
                <td>{{ item.name }}</td>
                <td>
                  <el-tag v-if="item.status === 'pending'" type="info">待处理</el-tag>
                  <el-tag v-else-if="item.status === 'processing'" type="warning">处理中</el-tag>
                  <el-tag v-else-if="item.status === 'success'" type="success">成功</el-tag>
                  <el-tag v-else-if="item.status === 'failed'" type="danger">失败</el-tag>
                </td>
                <td>
                      <el-space>
                        <el-button size="mini" @click="viewBatchResult(item)">查看</el-button>
                        <el-button size="mini" @click="downloadBatchItem(item)" :disabled="!item.result || !item.result.success">下载MD</el-button>
                        <el-button size="mini" @click="retryBatchItem(item)" v-if="item.status === 'failed'">重试</el-button>
                        <el-button size="mini" type="danger" @click="deleteBatchItem(item)">删除</el-button>
                      </el-space>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </el-card>
    </div>
    
  </div>
</template>

<style scoped lang="scss">
.preproc-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.preproc-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.preproc-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.preproc-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.preproc-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.preproc-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.page-title {
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 4px;
  color: var(--gray-900);
}
.component-intro {
  margin-top: 4px;
  margin-bottom: 8px;
  padding: 12px 14px;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: #f8fafc;
  color: var(--gray-700);
  line-height: 1.65;
  font-size: 14px;
}

.intro-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--gray-900);
  margin: 0 0 6px;
}

.component-intro p {
  margin: 6px 0;
}

.intro-steps {
  margin: 6px 0 0 20px;
  padding: 0;
}

.intro-steps li {
  margin: 4px 0;
}

.health-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.health-label {
  font-size: 14px;
  color: var(--gray-600);
}

.form-card {
  border-radius: var(--card-radius-lg);
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.file-layout {
  display: flex;
  gap: 20px;
}

.file-left,
.file-right {
  flex: 1;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  padding: 18px;
  background: #f8fafc;
  height: 400px; /* 固定高度 */
  overflow-y: auto; /* 超出内容显示滚动条 */
  transition: all 0.3s ease; /* 平滑过渡动画 */
}

.action-row {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.upload-tip {
  font-size: 12px;
  color: #64748b;
}

.preview-area {
  margin-top: 16px;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px;
  min-height: 210px;
}

.preview-image,
.preview-pdf {
  max-width: 100%;
  border-radius: 8px;
  display: block;
  margin: 0 auto;
}

.preview-text {
  white-space: pre-wrap;
  word-break: break-word;
  background: #f8fafc;
  max-width: 100%;
  max-height: 100%;
  padding: 12px;
  border-radius: 8px;
}

.preview-html-frame {
  width: 100%;
  min-height: 320px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
}

.preview-pdf-pages {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.pdf-page-wrapper {
  width: 100%;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  padding: 8px;
}

.pdf-page-canvas {
  display: block;
  width: 100%;
  height: auto;
}

.preview-word-hint {
  margin-bottom: 10px;
}

.preview-word {
  color: #111827;
  line-height: 1.7;
  font-size: 14px;
}

.preview-word :deep(h1),
.preview-word :deep(h2),
.preview-word :deep(h3),
.preview-word :deep(h4) {
  margin: 14px 0 10px;
  font-weight: 700;
}

.preview-word :deep(p) {
  margin: 8px 0;
}

.preview-word :deep(ul),
.preview-word :deep(ol) {
  margin: 8px 0 8px 20px;
}

.preview-word :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  background: #fff;
}

.preview-word :deep(th),
.preview-word :deep(td) {
  border: 1px solid #d1d5db;
  padding: 8px 10px;
  vertical-align: top;
}

.preview-word :deep(strong) {
  font-weight: 700;
}

.preview-word :deep(.word-formula-placeholder) {
  margin: 8px 0 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  color: #9a3412;
}

.excel-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.excel-tab {
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #f8fafc;
  color: #334155;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 13px;
}

.excel-tab.active {
  background: #2563eb;
  border-color: #2563eb;
  color: #fff;
}

.preview-excel {
  overflow-x: auto;
}

.preview-excel :deep(table) {
  width: 100%;
  border-collapse: collapse;
  table-layout: auto;
  font-size: 13px;
  background: #fff;
}

.preview-excel :deep(tr:nth-child(even) td) {
  background: #f8fafc;
}

.preview-excel :deep(th),
.preview-excel :deep(td) {
  border: 1px solid #cbd5e1;
  padding: 6px 10px;
  text-align: left;
  vertical-align: middle;
}

.preview-excel :deep(th) {
  background: #e2e8f0;
  font-weight: 700;
}

.preview-tip {
  color: #475569;
  padding: 12px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
}
.markdown-preview {
  line-height: 1.7;
  color: var(--gray-800);
  background: var(--gray-100);
  padding: 16px;
  border-radius: 8px;
}

.markdown-preview :deep(h1),
.markdown-preview :deep(h2),
.markdown-preview :deep(h3),
.markdown-preview :deep(h4),
.markdown-preview :deep(h5),
.markdown-preview :deep(h6) {
  margin: 16px 0 10px;
  line-height: 1.35;
  color: var(--gray-900);
}

.markdown-preview :deep(p),
.markdown-preview :deep(ul),
.markdown-preview :deep(ol),
.markdown-preview :deep(blockquote) {
  margin: 10px 0;
}

.markdown-preview :deep(code) {
  background: #f3f4f6;
  color: #111827;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.9em;
}

.markdown-preview :deep(pre) {
  margin: 12px 0;
  border-radius: 8px;
  overflow: auto;
  border: 1px solid #e5e7eb;
}

.markdown-preview :deep(pre code) {
  display: block;
  padding: 14px;
  background: #0f172a;
  color: #e2e8f0;
}

.markdown-preview :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  background: #fff;
}

.markdown-preview :deep(th),
.markdown-preview :deep(td) {
  border: 1px solid #d1d5db;
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}

.markdown-preview :deep(th) {
  background: #f9fafb;
  font-weight: 600;
}

.markdown-preview :deep(blockquote) {
  border-left: 4px solid #93c5fd;
  padding: 8px 12px;
  color: #4b5563;
  background: #eff6ff;
}

.placeholder {
  color: var(--gray-500);
  text-align: center;
  padding: 16px;
  border: 1px dashed var(--gray-300);
  border-radius: 8px;
}

.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-label {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--gray-800);
}
.result-meta {
  font-size: 13px;
  color: var(--gray-600);
  margin: 4px 0;
}
.result-meta code {
  font-size: 12px;
  background: var(--gray-200);
  padding: 2px 6px;
  border-radius: 4px;
}
.result-error {
  font-size: 14px;
  color: var(--el-color-danger);
  margin: 8px 0;
}
.result-content {
  margin-top: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  overflow: auto;
  max-height: 360px;
  background: #fff;
}
.result-content pre {
  margin: 0;
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}
.result-json {
  font-size: 12px;
  overflow: auto;
  max-height: 240px;
  margin: 0;
  white-space: pre-wrap;
}

.form-tip {
  margin-left: 10px;
  color: #909399;
  font-size: 12px;
}

.model-info-card {
  margin-top: 16px;
}

.model-details p {
  margin: 8px 0;
  line-height: 1.5;
}

.pros-cons {
  display: flex;
  gap: 20px;
  margin-top: 12px;
}

.pros-cons .pros {
  color: #67c23a;
  flex: 1;
}
.batch-list {
  margin-top: 12px;
}
.batch-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 8px;
}
.batch-table thead th {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-200);
  color: var(--gray-700);
  font-weight: 600;
}
.batch-table tbody td {
  padding: 8px 12px;
  border-bottom: 1px dashed var(--gray-100);
  vertical-align: middle;
}
.batch-table tr:hover td {
  background: rgba(0,0,0,0.02);
}

.pros-cons .cons {
  color: #f56c6c;
  flex: 1;
}

.pros-cons ul {
  margin: 4px 0 0 0;
  padding-left: 20px;
}

.pros-cons li {
  margin: 2px 0;
}

@media (max-width: 768px) {
  .preproc-view {
    padding: 24px 20px 60px;
  }
  .page-title {
    font-size: 22px;
  }
  .page-desc {
    font-size: 14px;
  }
}
</style>
