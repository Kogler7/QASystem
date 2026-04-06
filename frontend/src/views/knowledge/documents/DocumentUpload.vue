<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Upload, Loading, Link, DocumentCopy, Files, Scissor, Connection, DataLine, CircleCheck, Delete } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { preprocAcceptAttr, preprocFormatHint, isPreprocExtension } from '@/constants/preprocFormats'
import { normalizeToRawFiles } from '@/utils/preprocUploadLogic'
import { checkDocumentNameExists } from '@/api'
import {
  SEDIMENT_STORAGE_KEY,
  SEDIMENT_STORAGE_VERSION,
  readSedimentStorage,
  writeSedimentStorage,
  cancelCurrentSedimentRun,
  runKnowledgeSedimentPipeline,
  runKnowledgeSedimentPipelineBatch,
  fileToHint,
  SedimentRunCancelledError
} from '@/utils/knowledgeSedimentPipeline'

const uploadLoading = ref(false)
const pipelineRunning = ref(false)

const selectedFile = ref(null)
const convertedText = ref('')
const chunks = ref([])
const extractResult = ref(null)
const graphUpdateResult = ref(null)
const storedDocument = ref(null)
/** 预处理接口返回的摘要字段（不含长文本 content） */
const preprocMeta = ref(null)
/** 置信度计算结果，用于步骤预览 */
const kgComputeResult = ref(null)
/** 从 sessionStorage 恢复时记录上次文件名（无法恢复 File 本体） */
const fileHint = ref(null)

const stepIndex = ref(0)
const stepError = ref('')
const availableModels = ref({})
const modelsLoaded = ref(false)
const kgConfidenceThreshold = 0.7
const zipAcceptExt = '.zip'
const uploadAcceptAttr = `${preprocAcceptAttr},${zipAcceptExt}`
const uploadFormatHint = `${preprocFormatHint}；支持 ZIP（自动解压后顺序执行）`
const extractTypeOptions = [
  { value: 'Term', label: '术语' },
  { value: 'RuleType', label: '规约规则' },
  { value: 'SystemElement', label: '系统元素' }
]
const selectedExtractTypes = ref(['Term'])

const convertedCharCount = computed(() => (convertedText.value ? convertedText.value.length : 0))

const extractStats = computed(() => {
  const g = extractResult.value?.graph
  if (!g) return null
  return {
    nodes: (g.nodes || []).length,
    rels: (g.relationships || []).length,
    onto: (g.ontology_relations || []).length
  }
})

const kgComputeStats = computed(() => {
  const r = kgComputeResult.value
  if (!r || typeof r !== 'object') return null
  return {
    ok: r.success !== false,
    high: Array.isArray(r.relations_high) ? r.relations_high.length : 0,
    full: Array.isArray(r.full_relations) ? r.full_relations.length : 0
  }
})

const kgWriteStats = computed(() => {
  const r = graphUpdateResult.value
  if (!r || typeof r !== 'object') return null
  const s = r.statistics
  if (!s) {
    return {
      ok: Boolean(r.success),
      message: r.message || (r.success ? '已完成' : '未知状态')
    }
  }
  return {
    ok: r.success !== false && (s.failed == null || s.failed === 0),
    total: s.total,
    added: s.added,
    updated: s.updated,
    failed: s.failed,
    elapsed: s.elapsed_time
  }
})

/** 供「原始 JSON」折叠展示：预处理响应去掉超长 content，避免与上方预览重复 */
const preprocDebugJson = computed(() => {
  const m = preprocMeta.value
  if (!m) return ''
  try {
    return JSON.stringify(m, null, 2)
  } catch {
    return ''
  }
})

const steps = computed(() => [
  { title: '多源数据预处理', desc: '文档解析/清洗/统一为可处理文本', icon: Files },
  { title: '文本切分', desc: '按Markdown切分为片段，便于抽取与向量化', icon: Scissor },
  { title: '知识抽取', desc: '抽取实体、关系与结构化图数据', icon: Connection },
  { title: '图谱更新', desc: '将抽取结果写入 Neo4j（或增量更新）', icon: DataLine }
])

const statusFor = (i) => {
  if (stepError.value) return i < stepIndex.value ? 'success' : i === stepIndex.value ? 'error' : 'wait'
  if (pipelineRunning.value) return i < stepIndex.value ? 'success' : i === stepIndex.value ? 'process' : 'wait'
  return i < stepIndex.value ? 'success' : 'wait'
}

const overallProgress = computed(() => {
  const total = steps.value.length
  const done = Math.min(stepIndex.value, total)
  return Math.round((done / total) * 100)
})

/** 获取步骤间连接线的填充宽度（用于可视化进度） */
const getStepBarWidth = (i) => (i < stepIndex.value ? 100 : 0)

function buildSedimentSnapshot() {
  const f = selectedFile.value
  return {
    v: SEDIMENT_STORAGE_VERSION,
    convertedText: convertedText.value,
    chunks: chunks.value,
    extractResult: extractResult.value,
    graphUpdateResult: graphUpdateResult.value,
    storedDocument: storedDocument.value,
    preprocMeta: preprocMeta.value,
    kgComputeResult: kgComputeResult.value,
    stepIndex: stepIndex.value,
    stepError: stepError.value,
    modelsLoaded: modelsLoaded.value,
    availableModels: availableModels.value,
    pipelineRunning: pipelineRunning.value,
    uploadLoading: uploadLoading.value,
    selectedExtractTypes: selectedExtractTypes.value,
    fileHint:
      f && f.name
        ? fileToHint(f)
        : fileHint.value
  }
}

/**
 * 从存储或 runner 回调同步到界面（含执行中状态，便于离页返回后对齐）
 * @param {object} s
 * @param {{ resetFile?: boolean }} opts resetFile=true 时清空本地 File（仅用于从 sessionStorage 恢复）
 */
function applySedimentFields(s, opts = {}) {
  const resetFile = opts.resetFile === true
  if (!s || s.v !== SEDIMENT_STORAGE_VERSION) return false
  convertedText.value = s.convertedText || ''
  chunks.value = Array.isArray(s.chunks) ? s.chunks : []
  extractResult.value = s.extractResult ?? null
  graphUpdateResult.value = s.graphUpdateResult ?? null
  storedDocument.value = s.storedDocument ?? null
  preprocMeta.value = s.preprocMeta ?? null
  kgComputeResult.value = s.kgComputeResult ?? null
  stepIndex.value = typeof s.stepIndex === 'number' ? s.stepIndex : 0
  stepError.value = s.stepError || ''
  modelsLoaded.value = Boolean(s.modelsLoaded)
  availableModels.value = s.availableModels && typeof s.availableModels === 'object' ? s.availableModels : {}
  pipelineRunning.value = Boolean(s.pipelineRunning)
  uploadLoading.value = Boolean(s.uploadLoading)
  selectedExtractTypes.value = Array.isArray(s.selectedExtractTypes) && s.selectedExtractTypes.length
    ? s.selectedExtractTypes
    : ['Term']
  if (resetFile) {
    selectedFile.value = null
    fileHint.value = s.fileHint && s.fileHint.name ? s.fileHint : null
  } else if (s.fileHint?.name) {
    fileHint.value = s.fileHint
  }
  return true
}

function persistRefsToStorage() {
  writeSedimentStorage(buildSedimentSnapshot())
}

const resetPipeline = () => {
  // 取消当前 run，会阻止后续步骤继续执行/回写状态。
  cancelCurrentSedimentRun()
  pipelineRunning.value = false
  stepIndex.value = 0
  stepError.value = ''
  convertedText.value = ''
  chunks.value = []
  extractResult.value = null
  graphUpdateResult.value = null
  storedDocument.value = null
  preprocMeta.value = null
  kgComputeResult.value = null
  persistRefsToStorage()
}

/** 清除本地缓存 + 文件 + 全部流程数据 */
const clearAllSedimentState = () => {
  stopStoragePoll()
  cancelCurrentSedimentRun()
  try {
    sessionStorage.removeItem(SEDIMENT_STORAGE_KEY)
  } catch (_) {
    /* ignore */
  }
  fileHint.value = null
  selectedFile.value = null
  pipelineRunning.value = false
  uploadLoading.value = false
  stepIndex.value = 0
  stepError.value = ''
  convertedText.value = ''
  chunks.value = []
  extractResult.value = null
  graphUpdateResult.value = null
  storedDocument.value = null
  preprocMeta.value = null
  kgComputeResult.value = null
  ElMessage.success('已清除知识沉淀状态')
}

let _storagePollTimer = null
function stopStoragePoll() {
  if (_storagePollTimer != null) {
    clearInterval(_storagePollTimer)
    _storagePollTimer = null
  }
}

function startStoragePollIfRunning() {
  stopStoragePoll()
  const s0 = readSedimentStorage()
  if (!s0?.pipelineRunning) return
  _storagePollTimer = setInterval(() => {
    const s = readSedimentStorage()
    if (s) applySedimentFields(s, { resetFile: true })
    if (!s?.pipelineRunning) stopStoragePoll()
  }, 500)
}

const handlePick = async (file) => {
  const isZip = String(file?.name || '').toLowerCase().endsWith(zipAcceptExt)
  if (!isPreprocExtension(file?.name) && !isZip) {
    const parts = (file?.name || '').split('.')
    const ext = parts.length > 1 ? '.' + parts.pop().toLowerCase() : ''
    ElMessage.error(`不支持的格式「${ext || '无扩展名'}」。${uploadFormatHint}`)
    return
  }
  // ZIP 内文件名在解压后由流水线逐文件校验；此处仅对直接选中的单文件提前查重
  if (!isZip && file?.name) {
    try {
      const { data } = await checkDocumentNameExists(file.name)
      if (data?.exists) {
        ElMessage.error(`文件「${file.name}」已在知识库中存在，请勿重复沉淀`)
        return
      }
    } catch {
      ElMessage.warning('无法预检文件名是否重复，开始执行后将在入库步骤校验')
    }
  }
  selectedFile.value = file
  fileHint.value = null
  resetPipeline()
}

const onFileChange = (file) => {
  const raws = normalizeToRawFiles(file ? [file] : [])
  if (raws.length) handlePick(raws[0])
}

const runPipeline = async () => {
  if (!selectedFile.value) {
    ElMessage.warning('请先选择文档')
    return
  }
  if (!selectedExtractTypes.value.length) {
    ElMessage.warning('请至少选择一个知识抽取类型')
    return
  }
  stopStoragePoll()
  stepError.value = ''
  try {
    const summary = await runKnowledgeSedimentPipelineBatch({
      files: selectedFile.value,
      availableModels: availableModels.value,
      modelsLoaded: modelsLoaded.value,
      kgConfidenceThreshold,
      extractTypes: selectedExtractTypes.value,
      onUpdate: (snap) => applySedimentFields(snap, { resetFile: false })
    })
    if (summary.cancelled) return
    if (summary.total <= 1 && summary.failed === 0) {
      ElMessage.success('流程完成：已更新知识图谱并入库文档列表')
    } else if (summary.failed === 0) {
      ElMessage.success(`批量流程完成：成功 ${summary.success}/${summary.total}`)
    } else {
      ElMessage.warning(`批量流程完成：成功 ${summary.success}/${summary.total}，失败 ${summary.failed}`)
    }
  } catch (err) {
    if (err instanceof SedimentRunCancelledError) return
    const msg = err?.response?.data?.detail || err?.message || '执行失败'
    ElMessage.error(msg)
  } finally {
    stopStoragePoll()
  }
}

onMounted(() => {
  const s = readSedimentStorage()
  if (s && applySedimentFields(s, { resetFile: true })) startStoragePollIfRunning()
})

onUnmounted(() => {
  stopStoragePoll()
})

const copyConverted = async () => {
  if (!convertedText.value) return ElMessage.warning('暂无可复制文本')
  try {
    await navigator.clipboard.writeText(convertedText.value)
    ElMessage.success('已复制预处理文本')
  } catch {
    ElMessage.error('复制失败，请手动复制')
  }
}

const openNeo4jBrowser = () => {
  window.open('http://localhost:7474/browser/', '_blank')
}
</script>

<template>
  <div class="document-upload">
    <el-card class="upload-card" shadow="hover">
      <template #header>
        <div class="header-row">
          <span>上传文档</span>
        </div>
      </template>

      <el-upload
        drag
        :auto-upload="false"
        :show-file-list="false"
        :disabled="uploadLoading || pipelineRunning"
        @change="onFileChange"
        :accept="uploadAcceptAttr"
      >
        <el-icon class="upload-icon"><Upload /></el-icon>
        <div class="upload-text">将文件拖到此处，或<em>点击选择</em></div>
        <template #tip>
          <div class="upload-tip">{{ uploadFormatHint }}</div>
        </template>
      </el-upload>

      <div v-if="uploadLoading" class="upload-loading">
        <el-icon class="is-loading"><Loading /></el-icon>
        入库中...
      </div>

      <div v-if="selectedFile" class="picked-row">
        <span class="picked-label">当前文件：</span>
        <span class="picked-name">{{ selectedFile.name }}</span>
        <el-button link type="primary" @click="resetPipeline">重置流程</el-button>
        <el-button link type="danger" :icon="Delete" @click="clearAllSedimentState">清除全部状态</el-button>
      </div>
      <div v-else-if="convertedText || stepIndex > 0" class="picked-row picked-row--end">
        <el-button link type="danger" :icon="Delete" @click="clearAllSedimentState">清除全部状态</el-button>
      </div>
    </el-card>

    <el-card class="pipeline-card" shadow="hover">
      <template #header>
        <div class="header-row">
          <span>更新流程</span>
          <div class="header-actions">
            <el-button type="primary" :disabled="!selectedFile" :loading="pipelineRunning" @click="runPipeline">
              开始执行
            </el-button>
          </div>
        </div>
      </template>

      <div class="extract-type-row">
        <span class="extract-type-label">抽取类型</span>
        <el-checkbox-group v-model="selectedExtractTypes" :disabled="pipelineRunning">
          <el-checkbox
            v-for="opt in extractTypeOptions"
            :key="opt.value"
            :label="opt.value"
          >
            {{ opt.label }}
          </el-checkbox>
        </el-checkbox-group>
      </div>
      <p class="hint-text hint-text--top-space">
        将按所选类型分别执行“知识抽取”并合并结果后写入图谱。
      </p>

      <!-- 横向步骤进度可视化 -->
      <div class="step-progress-visual">
        <div class="step-progress-track">
          <template v-for="(s, i) in steps" :key="s.title">
            <div class="step-node" :class="['step-node--' + statusFor(i)]">
              <div class="step-node-circle">
                <el-icon v-if="statusFor(i) === 'success'" class="step-icon"><CircleCheck /></el-icon>
                <el-icon v-else class="step-icon"><component :is="s.icon" /></el-icon>
              </div>
              <div class="step-node-label">
                <span class="step-title">{{ s.title }}</span>
                <span class="step-desc">{{ s.desc }}</span>
              </div>
            </div>
            <div v-if="i < steps.length - 1" class="step-connector">
              <div class="step-connector-bg" />
              <div
                class="step-connector-fill"
                :style="{ width: getStepBarWidth(i) + '%' }"
              />
            </div>
          </template>
        </div>
      </div>

      <!-- 整体进度条 -->
      <div class="progress-row">
        <div class="progress-meta">
          <span class="progress-label">整体进度</span>
          <span class="progress-value">{{ overallProgress }}%</span>
        </div>
        <el-progress
          :percentage="overallProgress"
          :status="stepError ? 'exception' : stepIndex >= steps.length ? 'success' : undefined"
          :stroke-width="12"
          :show-text="false"
        />
        <p v-if="stepError" class="error-text">失败原因：{{ stepError }}</p>
      </div>

      <el-divider />

      <el-collapse class="detail-collapse">
        <el-collapse-item title="预处理输出（文本预览）" name="preproc">
          <div v-if="convertedText" class="preview-summary">
            <el-tag size="small" type="info">文本长度 {{ convertedCharCount }} 字符</el-tag>
            <el-tag v-if="preprocMeta?.success === false" size="small" type="danger">预处理标记失败</el-tag>
          </div>
          <div class="detail-actions">
            <el-button :icon="DocumentCopy" size="small" @click="copyConverted">复制全文</el-button>
          </div>
          <pre class="preview-block">{{ convertedText || '尚未执行预处理' }}</pre>
          <el-collapse v-if="preprocMeta" class="raw-json-collapse">
            <el-collapse-item title="预处理接口原始字段（调试用，不含正文）" name="preproc-raw">
              <pre class="preview-block preview-block--compact">{{ preprocDebugJson }}</pre>
            </el-collapse-item>
          </el-collapse>
        </el-collapse-item>
        <el-collapse-item title="切分结果（片段预览）" name="split">
          <div v-if="chunks.length" class="preview-summary">
            <el-tag size="small" type="info">共 {{ chunks.length }} 个片段</el-tag>
            <el-tag size="small" type="success">展示前 {{ Math.min(50, chunks.length) }} 条</el-tag>
          </div>
          <el-table :data="chunks.slice(0, 50)" size="small" border max-height="240">
            <el-table-column type="index" label="#" width="56" />
            <el-table-column label="片段内容">
              <template #default="{ row }">
                <span class="chunk-text">{{ typeof row === 'string' ? row : row?.text ?? JSON.stringify(row) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <p class="hint-text">切分结果会作为切块 JSON 送入知识抽取；块数过多时仅表格预览前 50 条。</p>
        </el-collapse-item>
        <el-collapse-item title="知识抽取结果" name="extract">
          <div v-if="extractStats" class="preview-summary">
            <el-tag size="small" type="info">节点 {{ extractStats.nodes }}</el-tag>
            <el-tag size="small" type="info">关系 {{ extractStats.rels }}</el-tag>
            <el-tag v-if="extractStats.onto" size="small" type="warning">本体边 {{ extractStats.onto }}</el-tag>
          </div>
          <p v-else class="hint-text">尚未执行抽取</p>
          <el-collapse v-if="extractResult" class="raw-json-collapse">
            <el-collapse-item title="原始 JSON（raw / graph）" name="extract-raw">
              <pre class="preview-block">{{ JSON.stringify(extractResult, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </el-collapse-item>
        <el-collapse-item title="图谱更新（Neo4j）" name="graph-view">
          <div class="graph-view-block">
            <el-button
              type="primary"
              :icon="Link"
              :loading="pipelineRunning"
              :disabled="stepIndex < 4"
              @click="openNeo4jBrowser"
            >
              打开 Neo4j Browser
            </el-button>
            <span class="hint-text">完成「图谱更新」步骤后可查看可视化。</span>
          </div>

          <div v-if="kgComputeStats" class="preview-summary preview-summary--spaced">
            <span class="summary-label">置信度筛选</span>
            <el-tag size="small" :type="kgComputeStats.ok ? 'success' : 'danger'">
              {{ kgComputeStats.ok ? '计算成功' : '失败' }}
            </el-tag>
            <el-tag size="small" type="info">高置信度 {{ kgComputeStats.high }} 条</el-tag>
            <el-tag size="small" type="info">待评关系 {{ kgComputeStats.full }} 条</el-tag>
          </div>

          <div v-if="kgWriteStats" class="neo4j-result-card">
            <div class="neo4j-result-title">写入 Neo4j</div>
            <el-alert
              :type="
                kgWriteStats.failed > 0
                  ? 'warning'
                  : kgWriteStats.ok
                    ? 'success'
                    : 'error'
              "
              :closable="false"
              show-icon
            >
              <template v-if="kgWriteStats.total != null">
                <p class="neo4j-result-line">
                  合计 <strong>{{ kgWriteStats.total }}</strong> 条关系：新增
                  <strong>{{ kgWriteStats.added }}</strong>，更新
                  <strong>{{ kgWriteStats.updated }}</strong>，失败
                  <strong>{{ kgWriteStats.failed }}</strong>
                  <span v-if="kgWriteStats.elapsed != null">，耗时 {{ kgWriteStats.elapsed }}s</span>
                </p>
              </template>
              <template v-else>
                <p class="neo4j-result-line">{{ kgWriteStats.message }}</p>
              </template>
            </el-alert>
          </div>

          <el-collapse v-if="graphUpdateResult" class="raw-json-collapse">
            <el-collapse-item title="写入接口原始 JSON（调试）" name="graph-raw">
              <pre class="preview-block">{{ JSON.stringify(graphUpdateResult, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>

          <el-collapse v-if="kgComputeResult" class="raw-json-collapse">
            <el-collapse-item title="置信度计算原始 JSON（调试）" name="kg-compute-raw">
              <pre class="preview-block">{{ JSON.stringify(kgComputeResult, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>

          <p v-if="storedDocument" class="update-hint doc-stored">
            已加入文档列表：<strong>{{ storedDocument.name || storedDocument.filename || '-' }}</strong>
          </p>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.document-upload {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.upload-card,
.pipeline-card {
  border-radius: var(--card-radius);
}

.upload-icon {
  font-size: 48px;
  color: var(--primary-400);
}

.upload-text {
  margin-top: 8px;
  font-size: 14px;
  color: var(--gray-600);
  em {
    color: var(--primary-500);
    font-style: normal;
  }
}

.upload-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gray-500);
}

.upload-loading {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--gray-600);
}

.picked-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--gray-700);
}

.picked-row--end {
  justify-content: flex-end;
}
.picked-label {
  color: var(--gray-500);
}
.picked-name {
  font-weight: 600;
}

.step-progress-visual {
  margin: 24px 0;
  padding: 16px 0;
}

.step-progress-track {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0;
  position: relative;
}

.step-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 0 0 auto;
  z-index: 1;

  &.step-node--success .step-node-circle {
    background: var(--el-color-success);
    color: #fff;
    border-color: var(--el-color-success);
  }

  &.step-node--process .step-node-circle {
    background: var(--primary-500);
    color: #fff;
    border-color: var(--primary-500);
    animation: pulse-step 1.5s ease-in-out infinite;
  }

  &.step-node--error .step-node-circle {
    background: var(--el-color-danger);
    color: #fff;
    border-color: var(--el-color-danger);
  }

  &.step-node--wait .step-node-circle {
    background: var(--gray-100);
    color: var(--gray-500);
    border-color: var(--gray-300);
  }
}

.step-node-circle {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid;
  transition: all 0.3s ease;
}

.step-icon {
  font-size: 22px;
}

.step-node-label {
  margin-top: 10px;
  text-align: center;
  max-width: 140px;
}

.step-title {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-800);
}

.step-desc {
  display: block;
  font-size: 11px;
  color: var(--gray-500);
  margin-top: 2px;
  line-height: 1.3;
}

.step-connector {
  flex: 1;
  min-width: 40px;
  height: 24px;
  position: relative;
  margin-top: 12px;
  align-self: flex-start;
}

.step-connector-bg {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  height: 4px;
  background: var(--gray-200);
  border-radius: 2px;
  transform: translateY(-50%);
}

.step-connector-fill {
  position: absolute;
  top: 50%;
  left: 0;
  height: 4px;
  background: var(--el-color-success);
  border-radius: 2px;
  transform: translateY(-50%);
  transition: width 0.4s ease;
}

@keyframes pulse-step {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.85; }
}

.progress-row {
  margin-top: 16px;
}

.extract-type-row {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.extract-type-label {
  font-size: 13px;
  color: var(--gray-600);
}

.hint-text--top-space {
  margin-top: 8px;
}
.progress-meta {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
}
.progress-label {
  font-size: 13px;
  color: var(--gray-600);
}
.progress-value {
  font-size: 13px;
  color: var(--gray-700);
  font-weight: 600;
}
.error-text {
  margin: 10px 0 0;
  color: var(--el-color-danger);
  font-size: 13px;
}

.detail-collapse {
  margin-top: 10px;
}

.detail-actions {
  margin-bottom: 10px;
  display: flex;
  justify-content: flex-end;
}

.preview-block {
  margin: 0;
  padding: 12px;
  background: var(--gray-100);
  border-radius: 8px;
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.chunk-text {
  font-size: 12px;
  color: var(--gray-800);
}

.hint-text {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--gray-500);
}

.graph-view-block {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.update-hint {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--gray-600);
}

.doc-stored {
  margin-top: 14px;
}

.preview-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.preview-summary--spaced {
  margin-top: 12px;
}

.summary-label {
  font-size: 12px;
  color: var(--gray-500);
  margin-right: 4px;
}

.raw-json-collapse {
  margin-top: 12px;
  border: none;
  :deep(.el-collapse-item__header) {
    font-size: 12px;
    color: var(--gray-600);
    height: 36px;
    line-height: 36px;
  }
}

.preview-block--compact {
  max-height: 200px;
}

.neo4j-result-card {
  margin-top: 12px;
}

.neo4j-result-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-800);
  margin-bottom: 8px;
}

.neo4j-result-line {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--gray-800);
}

@media (max-width: 900px) {
  .step-progress-track {
    flex-wrap: wrap;
    justify-content: center;
    gap: 20px;
  }

  .step-connector {
    display: none;
  }

  .step-node {
    flex: 0 0 calc(50% - 10px);
    max-width: 160px;
  }
}
</style>
