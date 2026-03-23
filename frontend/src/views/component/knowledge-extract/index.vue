<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Search, Link, Upload, DocumentCopy, Download, FolderOpened } from '@element-plus/icons-vue'
import { knowledgeExtract, extractHealthCheck, storeGraphToNeo4j, parseChunkedJsonExtract } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const chunkJsonLoading = ref(false)
const storeLoading = ref(false)
const chunkJsonInputRef = ref(null)
const healthStatus = ref(null)
const mainObjectOptions = [
  { value: 'Term', label: 'Term（术语）' },
  { value: 'RuleType', label: 'RuleType（规约规则）' },
  { value: 'SystemElement', label: 'SystemElement（系统元素）' }
]

const form = reactive({
  main_object: 'Term',
  text: '',
  use_templates: true
})

const result = ref(null)

const submit = async () => {
  const { main_object, text } = form
  if (!main_object?.trim()) {
    ElMessage.warning('请选择主对象类型')
    return
  }
  if (!text?.trim()) {
    ElMessage.warning('请输入待抽取文本')
    return
  }
  loading.value = true
  result.value = null
  try {
    const { data } = await knowledgeExtract({
      main_object: main_object.trim(),
      text: text.trim(),
      use_templates: form.use_templates
    })
    result.value = data
    ElMessage.success('抽取完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '抽取失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

const storeToNeo4j = async () => {
  if (!result.value?.graph) {
    ElMessage.warning('请先完成知识抽取')
    return
  }
  storeLoading.value = true
  try {
    await storeGraphToNeo4j({ graph: result.value.graph })
    ElMessage.success('已成功写入 Neo4j')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '写入 Neo4j 失败')
  } finally {
    storeLoading.value = false
  }
}

/** 与接口返回一致：{ raw, graph } */
const buildResultPayload = () => {
  if (!result.value) return null
  return {
    raw: result.value.raw,
    graph: result.value.graph ?? {}
  }
}

const copyResult = async () => {
  const payload = buildResultPayload()
  if (!payload) {
    ElMessage.warning('请先完成知识抽取或上传解析')
    return
  }
  try {
    await navigator.clipboard.writeText(JSON.stringify(payload, null, 2))
    ElMessage.success('已复制到剪贴板')
  } catch (err) {
    ElMessage.error('复制失败，请手动复制')
  }
}

const downloadResult = () => {
  const payload = buildResultPayload()
  if (!payload) {
    ElMessage.warning('请先完成知识抽取或上传解析')
    return
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `knowledge-extract-result.json`
  a.click()
  URL.revokeObjectURL(url)
}

const pickChunkJson = () => {
  chunkJsonInputRef.value?.click()
}

const onChunkJsonFile = async (e) => {
  const input = e.target
  const file = input?.files?.[0]
  if (!file) return
  const formData = new FormData()
  formData.append('file', file)
  formData.append('main_object', form.main_object.trim())
  formData.append('use_templates', String(form.use_templates))
  chunkJsonLoading.value = true
  result.value = null
  try {
    const { data } = await parseChunkedJsonExtract(formData)
    result.value = data
    ElMessage.success('切块 JSON 已解析并完成抽取')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '切块 JSON 解析失败')
    result.value = null
  } finally {
    chunkJsonLoading.value = false
    input.value = ''
  }
}

const NEO4J_BROWSER_URL = 'http://localhost:7474/browser/'

const healthDetail = ref('')
const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await extractHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '抽取模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端是否启动并安装 algorithm/uie 依赖'
  }
}

function openNeo4jBrowser() {
  window.open(NEO4J_BROWSER_URL, '_blank')
}

onMounted(checkHealth)
</script>

<template>
  <div class="extract-view">
    <div class="extract-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="extract-inner">
      <h1 class="page-title">
        <span class="title-highlight">知识抽取</span>
      </h1>
      <p class="page-desc">
        从文本中抽取指定类型的实体与关系，支持模板匹配，抽取结果可存储到 Neo4j 知识图谱。
      </p>

      <!-- 组件功能说明 -->
      <el-card class="func-desc-card" shadow="hover">
        <template #header>
          <span>组件功能说明</span>
        </template>
        <div class="func-desc-content">
          <p><strong>功能概述：</strong>本组件基于 UML 本体驱动的大模型信息抽取，将自然语言文本（如规则说明、系统说明、术语表等）抽取为结构化 JSON 及图结构。</p>
          <p><strong>主对象类型：</strong></p>
          <ul>
            <li><strong>Term</strong>：术语类知识抽取</li>
            <li><strong>RuleType</strong>：规约规则类知识抽取</li>
            <li><strong>SystemElement</strong>：系统元素类知识抽取</li>
          </ul>
          <p><strong>使用流程：</strong></p>
          <ol>
            <li>选择主对象类型，输入待抽取文本</li>
            <li>点击「开始抽取」执行抽取，或「上传切块 JSON」对文件中每段文本分别抽取</li>
            <li>「复制结果」「下载结果」导出与接口一致的 JSON</li>
            <li>确认结果无误后，点击「存储到 Neo4j」将图结构写入知识图谱</li>
            <li>可通过「查看图谱」跳转 Neo4j Browser 查看已存储的数据</li>
          </ol>
        </div>
      </el-card>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">抽取服务：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>抽取参数</span>
        </template>
        <el-form :model="form" label-width="120px" class="extract-form">
          <el-form-item label="主对象类型" required>
            <el-select
              v-model="form.main_object"
              placeholder="选择主对象类型"
              style="width: 100%"
            >
              <el-option
                v-for="opt in mainObjectOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="待抽取文本" required>
            <el-input
              v-model="form.text"
              type="textarea"
              :rows="5"
              placeholder="输入需要抽取知识的文本，例如：故障现象：列车在江泰路联锁区运营停车点不能自动取消。"
            />
          </el-form-item>
          <el-form-item label="启用模板匹配">
            <el-switch v-model="form.use_templates" />
            <span class="form-hint">默认开启，可提升抽取准确度</span>
          </el-form-item>
          <el-form-item>
            <div class="form-actions-row">
              <el-button
                type="primary"
                :loading="loading"
                :icon="Search"
                @click="submit"
              >
                开始抽取
              </el-button>
              <el-button :icon="Link" @click="openNeo4jBrowser">
                查看图谱
              </el-button>
              <input
                ref="chunkJsonInputRef"
                type="file"
                accept=".json,application/json"
                class="hidden-file-input"
                @change="onChunkJsonFile"
              >
              <el-button
                :loading="chunkJsonLoading"
                :icon="FolderOpened"
                @click="pickChunkJson"
              >
                上传切块 JSON
              </el-button>
              <el-button
                v-if="result"
                :icon="DocumentCopy"
                @click="copyResult"
              >
                复制结果
              </el-button>
              <el-button
                v-if="result"
                :icon="Download"
                @click="downloadResult"
              >
                下载结果
              </el-button>
            </div>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- 抽取结果展示 -->
      <el-card v-if="result" class="result-card" shadow="hover">
        <template #header>
          <div class="result-header">
            <span>抽取结果</span>
            <div class="result-actions">
              <el-button
                type="primary"
                :loading="storeLoading"
                :icon="Upload"
                size="small"
                @click="storeToNeo4j"
              >
                存储到 Neo4j
              </el-button>
            </div>
          </div>
        </template>
        <el-tabs type="border-card">
          <el-tab-pane label="结构化 JSON (raw)">
            <pre class="result-json">{{ JSON.stringify(result.raw, null, 2) }}</pre>
          </el-tab-pane>
          <el-tab-pane label="图结构 ">
            <div class="graph-preview">
              <div v-if="result.graph?.nodes?.length" class="graph-section">
                <h4>节点 ({{ result.graph.nodes.length }})</h4>
                <el-table :data="result.graph.nodes" size="small" max-height="240" border>
                  <el-table-column prop="uid" label="uid" width="120" show-overflow-tooltip />
                  <el-table-column prop="label" label="label" width="100" />
                  <el-table-column label="properties">
                    <template #default="{ row }">
                      <pre class="props-cell">{{ JSON.stringify(row.properties || {}) }}</pre>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
              <div v-if="result.graph?.relationships?.length" class="graph-section">
                <h4>关系 ({{ result.graph.relationships.length }})</h4>
                <el-table :data="result.graph.relationships" size="small" max-height="200" border>
                  <el-table-column prop="from_uid" label="from" width="120" show-overflow-tooltip />
                  <el-table-column prop="to_uid" label="to" width="120" show-overflow-tooltip />
                  <el-table-column prop="type" label="type" />
                </el-table>
              </div>
              <div v-if="result.graph?.ontology_relations?.length" class="graph-section">
                <h4>本体关系 ({{ result.graph.ontology_relations.length }})</h4>
                <el-table :data="result.graph.ontology_relations" size="small" max-height="200" border>
                  <el-table-column prop="from_uid" label="from" width="120" show-overflow-tooltip />
                  <el-table-column prop="class_name" label="class_name" width="120" />
                  <el-table-column prop="type" label="type" />
                </el-table>
              </div>
              <p v-if="!result.graph?.nodes?.length && !result.graph?.relationships?.length && !result.graph?.ontology_relations?.length" class="graph-empty">
                无图结构数据
              </p>
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.extract-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.extract-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}

.extract-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}

.extract-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}

.extract-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.extract-inner {
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

.page-desc {
  font-size: 15px;
  color: var(--gray-600);
  margin-bottom: 8px;
}

.func-desc-card {
  border-radius: var(--card-radius-lg);
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.func-desc-content {
  font-size: 14px;
  color: var(--gray-700);
  line-height: 1.7;
  p { margin: 0 0 8px; }
  ul, ol { margin: 4px 0 12px; padding-left: 22px; }
  li { margin-bottom: 4px; }
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

.extract-form {
  max-width: 640px;
}

.form-hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--gray-500);
}

.form-actions-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.hidden-file-input {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
}

.result-card {
  border-radius: var(--card-radius-lg);
}

.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.result-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.result-json {
  margin: 0;
  padding: 12px;
  background: var(--gray-100);
  border-radius: 6px;
  font-size: 13px;
  max-height: 360px;
  overflow: auto;
}

.graph-preview {
  .graph-section {
    margin-bottom: 16px;
    &:last-child { margin-bottom: 0; }
  }
  h4 {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--gray-800);
  }
}

.props-cell {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-width: 280px;
}

.graph-empty {
  color: var(--gray-500);
  font-size: 14px;
  margin: 0;
}

@media (max-width: 768px) {
  .extract-view {
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
