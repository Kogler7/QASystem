<script setup>
import { ref, onMounted, onBeforeUnmount, computed, watch, nextTick } from 'vue'
import { Upload, Document } from '@element-plus/icons-vue'
import { documentPreprocHealthCheck, documentPreprocConvert, getAvailableModels } from '@/api'
import { ElMessage } from 'element-plus'
import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import * as mammoth from 'mammoth/mammoth.browser'
import * as XLSX from 'xlsx'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'

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
const fileInput = ref(null)
const previewAreaRef = ref(null)
const pdfContainerRef = ref(null)
const result = ref(null)
const availableModels = ref({})
const selectedDataType = ref('')
const selectedModel = ref('')
const previewContent = ref('')
const previewType = ref('') // 'image', 'pdf', 'text', 'docx', 'excel', 'doc', 'other'
const wordHint = ref('')
const excelSheets = ref([])
const activeExcelSheet = ref(0)
const currentPdfFile = ref(null)

const acceptTypes = '.pdf,.docx,.doc,.xlsx,.xls,.csv,.txt,.jpg,.jpeg,.png,.gif,.bmp'
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
  const files = uploadFiles
  if (files && files.length && files[0].raw) {
    // el-upload 组件
    fileList.value = [files[0].raw]
  } else if (files && files.length) {
    // input[type=file] 方式
    fileList.value = [files[0]]
  } else {
    fileList.value = []
  }
  // 自动匹配数据类型
  if (fileList.value.length > 0) {
    const file = fileList.value[0]
    const ext = '.' + file.name.split('.').pop().toLowerCase()
    if (dataTypes.value.includes(ext)) {
      selectedDataType.value = ext
    }
    await generatePreview(file)
  } else {
    resetPreviewState()
  }
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
    previewType.value = 'doc'
    previewContent.value = 'DOC 为二进制格式，当前仅支持更高质量的 DOCX 预览。建议先转换为 PDF 或 DOCX。'
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
  if (!fileList.value?.length) {
    ElMessage.warning('请先选择文件')
    return
  }
  if (!selectedDataType.value) {
    ElMessage.warning('请选择数据类型')
    return
  }
  if (!selectedModel.value) {
    ElMessage.warning('请选择解析模型')
    return
  }
  await autoParse()
}

const autoParse = async () => {
  const file = fileList.value[0]
  loading.value = true
  result.value = null
  try {
    const { data } = await documentPreprocConvert(file, selectedModel.value)
    result.value = data
    if (data?.success) {
      ElMessage.success('转换完成')
    } else {
      ElMessage.warning(data?.error || '转换未成功')
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '转换失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

const contentDisplay = () => {
  if (!result.value) return ''
  const c = result.value.content_preview ?? result.value.content
  if (c == null) return ''
  return typeof c === 'string' ? c : JSON.stringify(c)
}

const renderedMarkdown = computed(() => {
  if (!result.value) return ''
  const content = contentDisplay()
  if (!content) return ''
  
  // 使用marked库渲染Markdown
  try {
    return marked.parse(content)
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

const onReplaceFile = () => {
  fileList.value = []
  resetPreviewState()
  // 触发隐藏 input 的点击
  openFilePicker()
}

const openFilePicker = () => {
  fileInput.value && fileInput.value.click()
}

let resizeTimer = null
const handleWindowResize = () => {
  if (previewType.value !== 'pdf' || !currentPdfFile.value) return
  window.clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(() => {
    renderPdfPreview(currentPdfFile.value)
  }, 180)
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
        <p><strong>处理对象类型：</strong>PDF、DOCX、DOC、XLSX、XLS、CSV等常见文件。</p>
        <p><strong>使用流程：</strong></p>
        <ol class="intro-steps">
          <li>点击“上传文件”选择待处理文档。</li>
          <li>根据文件类型选择“数据类型”和“解析模型”。</li>
          <li>点击“开始解析”发起文档转换。</li>
          <li>在右侧查看解析后的 Markdown 内容并继续后续处理。</li>
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
                <div class="preview-tip">{{ previewContent }}</div>
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
          </div>
        </div>

        <div class="action-row">
          <el-button
            type="primary"
            size="default"
            :icon="Upload"
            @click="openFilePicker"
          >
            上传文件
          </el-button>
          <el-button
            type="primary"
            :loading="loading"
            :icon="Document"
            @click="submit"
          >
            开始解析
          </el-button>
          <el-button
            type="primary"
            size="default"
            :icon="Upload"
            @click="onReplaceFile"
          >
            更换文件
          </el-button>
          <span class="upload-tip">支持 PDF、DOCX、DOC、XLSX、XLS、CSV</span>
        </div>
        <input
          ref="fileInput"
          type="file"
          :accept="acceptTypes"
          style="display: none"
          @change="handleChange($event.target.files[0], $event.target.files)"
        />
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
