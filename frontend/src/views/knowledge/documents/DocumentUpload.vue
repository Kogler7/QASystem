<script setup>
import { ref, computed } from 'vue'
import { Upload, Loading, Link, DocumentCopy, Files, Scissor, Connection, DataLine, CircleCheck } from '@element-plus/icons-vue'
import {
  uploadDocument,
  documentPreprocConvert,
  knowledgeExtract,
  nl2cypherSplitMarkdown,
  getAvailableModels,
  processSchemaOutput,
  addFromComputed
} from '@/api'
import { ElMessage } from 'element-plus'

const uploadLoading = ref(false)
const pipelineRunning = ref(false)

const selectedFile = ref(null)
const convertedText = ref('')
const chunks = ref([])
const extractResult = ref(null)
const graphUpdateResult = ref(null)
const storedDocument = ref(null)

const stepIndex = ref(0)
const stepError = ref('')
const availableModels = ref({})
const modelsLoaded = ref(false)
const kgConfidenceThreshold = 0.7

const steps = computed(() => [
  { title: '多源数据预处理', desc: '文档解析/清洗/统一为可处理文本', icon: Files },
  { title: '文本切分', desc: '按段落/标题切分为片段，便于抽取与向量化', icon: Scissor },
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

const resetPipeline = () => {
  pipelineRunning.value = false
  stepIndex.value = 0
  stepError.value = ''
  convertedText.value = ''
  chunks.value = []
  extractResult.value = null
  graphUpdateResult.value = null
  storedDocument.value = null
}

const handlePick = (file) => {
  selectedFile.value = file
  resetPipeline()
}

const onFileChange = (file) => {
  if (file?.raw) handlePick(file.raw)
}

const runPipeline = async () => {
  if (!selectedFile.value) {
    ElMessage.warning('请先选择文档')
    return
  }
  pipelineRunning.value = true
  stepError.value = ''
  stepIndex.value = 0
  try {
    // 1) 多源数据预处理
    if (!modelsLoaded.value) {
      try {
        const { data } = await getAvailableModels()
        availableModels.value = data || {}
      } catch {
        // 模型选择是可选的：失败则回退到后端默认模型
        availableModels.value = {}
      } finally {
        modelsLoaded.value = true
      }
    }

    const ext = '.' + (selectedFile.value?.name || '').split('.').pop().toLowerCase()
    const modelName = availableModels.value?.[ext]?.default || null

    const { data: preproc } = await documentPreprocConvert(selectedFile.value, modelName)
    convertedText.value = preproc?.text ?? preproc?.markdown ?? preproc?.content ?? ''
    if (!convertedText.value?.trim()) throw new Error('预处理未返回有效文本')
    stepIndex.value = 1

    // 2) 文本切分（预留：后续可替换为后端统一切分服务）
    const { data: splitRes } = await nl2cypherSplitMarkdown(convertedText.value)
    chunks.value = splitRes?.chunks ?? splitRes?.items ?? splitRes?.results ?? []
    if (!Array.isArray(chunks.value) || chunks.value.length === 0) {
      chunks.value = convertedText.value.split(/\n{2,}/).filter(Boolean).map((t) => t.trim())
    }
    stepIndex.value = 2

    // 3) 知识抽取（当前以全文为输入；后续可按 chunks 分批抽取）
    const { data: extract } = await knowledgeExtract({
      text: convertedText.value,
      // 兼容后端可能要求的字段（后续迭代可统一收敛到后端配置）
      main_object: 'Term',
      use_templates: true
    })
    extractResult.value = extract
    stepIndex.value = 3

    // 4) 图谱更新（预留：可改为三元组增量更新；当前直接写入 graph）
    if (!extractResult.value?.graph) throw new Error('抽取结果缺少 graph，无法更新图谱')

    // 使用 KGUpdate 新置信度/筛选/入库流程替代直接 storeGraphToNeo4j
    const { data: kgProcessRes } = await processSchemaOutput(extractResult.value, kgConfidenceThreshold)
    if (!kgProcessRes?.success) throw new Error(kgProcessRes?.message || 'KG 置信度计算失败')

    const { data: kgAddRes } = await addFromComputed(
      kgProcessRes.relations_high,
      kgProcessRes.full_relations,
      kgProcessRes.predictions,
      kgConfidenceThreshold
    )
    graphUpdateResult.value = kgAddRes ?? { status: 'ok' }
    stepIndex.value = 4

    // 完整流程成功后再写入“文档列表”（避免仅上传就出现在列表）
    uploadLoading.value = true
    try {
      const formData = new FormData()
      formData.append('file', selectedFile.value)
      const { data: stored } = await uploadDocument(formData)
      storedDocument.value = stored
      ElMessage.success('流程完成：已更新知识图谱并入库文档列表')
    } finally {
      uploadLoading.value = false
    }
  } catch (err) {
    stepError.value = err?.response?.data?.detail || err?.message || '执行失败'
    ElMessage.error(stepError.value)
  } finally {
    pipelineRunning.value = false
  }
}

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
        accept=".pdf,.doc,.docx,.txt,.md"
      >
        <el-icon class="upload-icon"><Upload /></el-icon>
        <div class="upload-text">将文件拖到此处，或<em>点击选择</em></div>
        <template #tip>
          <div class="upload-tip">支持 pdf、docx、xlsx等格式</div>
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
          <div class="detail-actions">
            <el-button :icon="DocumentCopy" size="small" @click="copyConverted">复制文本</el-button>
          </div>
          <pre class="preview-block">{{ convertedText || '尚未执行预处理' }}</pre>
        </el-collapse-item>
        <el-collapse-item title="切分结果（片段预览）" name="split">
          <el-table :data="chunks.slice(0, 50)" size="small" border max-height="240">
            <el-table-column type="index" label="#" width="56" />
            <el-table-column label="片段内容">
              <template #default="{ row }">
                <span class="chunk-text">{{ typeof row === 'string' ? row : row?.text ?? JSON.stringify(row) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <p class="hint-text">当前仅展示前 50 条；后续可按片段分批抽取与入库。</p>
        </el-collapse-item>
        <el-collapse-item title="知识抽取结果（graph/raw）" name="extract">
          <pre class="preview-block">{{ extractResult ? JSON.stringify(extractResult, null, 2) : '尚未执行抽取' }}</pre>
        </el-collapse-item>
        <el-collapse-item title="图谱更新（查看 Neo4j）" name="graph-view">
          <div class="graph-view-block">
            <el-button
              type="primary"
              :icon="Link"
              :loading="pipelineRunning"
              :disabled="stepIndex < 4"
              @click="openNeo4jBrowser"
            >
              查看图谱
            </el-button>
            <span class="hint-text">完成“图谱更新”步骤后即可查看。</span>
          </div>

          <p v-if="graphUpdateResult" class="update-hint">
            已写入 Neo4j（结果）：{{ typeof graphUpdateResult === 'string' ? graphUpdateResult : JSON.stringify(graphUpdateResult) }}
          </p>
          <p v-if="storedDocument" class="update-hint">
            已入库文档列表：{{ storedDocument.name || storedDocument.filename || '-' }}
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
