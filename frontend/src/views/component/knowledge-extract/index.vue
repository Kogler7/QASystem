<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Search, Link, Upload, DocumentCopy, Download, FolderOpened } from '@element-plus/icons-vue'
import { knowledgeExtract, extractHealthCheck, getUmlMainObjects, storeGraphToNeo4j, parseChunkedJsonExtract, uploadUmlAndParse, initKnowledgeSpace, getDimensionScales, knowledgeExtractV2, storeExtractionToNeo4j } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const chunkJsonLoading = ref(false)
const umlLoading = ref(false)
const storeLoading = ref(false)
const chunkJsonInputRef = ref(null)
const umlInputRef = ref(null)
const healthStatus = ref(null)
const mainObjectOptions = ref([])

const form = reactive({
  main_object: '',
  text: '',
  use_templates: true
})
const uploadForm = reactive({
  new_main_object: ''
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

const pickUmlFile = () => {
  umlInputRef.value?.click()
}

const onUmlFile = async (e) => {
  const input = e.target
  const file = input?.files?.[0]
  if (!file) return
  const lowerName = String(file.name || '').toLowerCase()
  if (!lowerName.endsWith('.puml')) {
    ElMessage.warning('仅支持上传 .puml 文件')
    input.value = ''
    return
  }
  const formData = new FormData()
  formData.append('file', file)
  if (!uploadForm.new_main_object?.trim()) {
    ElMessage.warning('请输入新增主对象名称')
    input.value = ''
    return
  }
  formData.append('new_main_object', uploadForm.new_main_object.trim())
  umlLoading.value = true
  result.value = null
  try {
    const { data } = await uploadUmlAndParse(formData)
    result.value = {
      raw: data?.raw || {},
      graph: data?.graph || { nodes: [], relationships: [] }
    }
    const savedFiles = Array.isArray(data?.saved_files) ? data.saved_files : []
    const addedMainObject = uploadForm.new_main_object.trim()
    if (savedFiles.length > 0) {
      ElMessage.success(`UML 已新增主对象并解析，且已写入 Neo4j（${savedFiles.join('，')}）`)
    } else {
      ElMessage.success('UML 已新增主对象并解析，且已写入 Neo4j')
    }
    const updated = Array.isArray(data?.main_objects) ? data.main_objects : []
    if (updated.length > 0) {
      mainObjectOptions.value = updated.map((it) => ({
        value: it.main_object,
        label: `${it.main_object}（${it.file_name}）`
      }))
      if (!form.main_object && updated[0]?.main_object) {
        form.main_object = updated[0].main_object
      }
      form.main_object = addedMainObject
      uploadForm.new_main_object = ''
    } else {
      await loadMainObjects()
      form.main_object = addedMainObject
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'UML 上传解析失败')
    result.value = null
  } finally {
    umlLoading.value = false
    input.value = ''
  }
}

// ── 知识空间初始化 ──
const ksInput = ref('')
const ksLoading = ref(false)
const ksResult = ref(null)

const initKS = async () => {
  // 解析输入格式: 维度1:刻度1,刻度2; 维度2:刻度3,刻度4
  // 也支持多行
  const text = (ksInput.value || '').trim()
  if (!text) {
    ElMessage.warning('请输入维度-刻度映射')
    return
  }
  const dimension_scales = {}
  const lines = text.split(/[\n;；]/)
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const colonIdx = trimmed.indexOf(':')
    if (colonIdx === -1) {
      ElMessage.warning(`格式错误，应为 "维度:刻度1,刻度2": ${trimmed}`)
      return
    }
    const dim = trimmed.substring(0, colonIdx).trim()
    const scalesStr = trimmed.substring(colonIdx + 1).trim()
    if (!dim || !scalesStr) {
      ElMessage.warning(`维度或刻度不能为空: ${trimmed}`)
      return
    }
    const scales = scalesStr.split(/[,，]/).map(s => s.trim()).filter(Boolean)
    if (!scales.length) {
      ElMessage.warning(`刻度列表为空: ${trimmed}`)
      return
    }
    dimension_scales[dim] = scales
  }
  if (!Object.keys(dimension_scales).length) {
    ElMessage.warning('请输入至少一个维度-刻度对')
    return
  }
  ksLoading.value = true
  ksResult.value = null
  try {
    const { data } = await initKnowledgeSpace({ dimension_scales })
    ksResult.value = data
    ElMessage.success(data.message || '知识空间初始化完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '初始化失败')
    ksResult.value = null
  } finally {
    ksLoading.value = false
  }
}

// ── 维度刻度查询 ──
const dimQueryInput = ref('')
const dimQueryLoading = ref(false)
const dimQueryResult = ref(null)

const queryDimScales = async () => {
  const dim = (dimQueryInput.value || '').trim()
  if (!dim) {
    ElMessage.warning('请输入维度名称')
    return
  }
  dimQueryLoading.value = true
  dimQueryResult.value = null
  try {
    const { data } = await getDimensionScales(dim)
    dimQueryResult.value = data
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '查询失败')
    dimQueryResult.value = null
  } finally {
    dimQueryLoading.value = false
  }
}

// ── 知识抽取 V2 ──
const v2Form = reactive({
  ontology: '',
  text: '',
  model: '',
  base_url: ''
})
const v2Loading = ref(false)
const v2StoreLoading = ref(false)
const v2Result = ref(null)

const extractV2 = async () => {
  const { ontology, text } = v2Form
  if (!ontology?.trim()) {
    ElMessage.warning('请输入本体定义（PUML）')
    return
  }
  if (!text?.trim()) {
    ElMessage.warning('请输入待抽取文本')
    return
  }
  v2Loading.value = true
  v2Result.value = null
  try {
    const { data } = await knowledgeExtractV2({
      ontology: ontology.trim(),
      text: text.trim(),
      model: v2Form.model || undefined,
      base_url: v2Form.base_url || undefined
    })
    v2Result.value = data
    ElMessage.success('知识抽取 V2 完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '抽取失败')
    v2Result.value = null
  } finally {
    v2Loading.value = false
  }
}

const storeV2ToNeo4j = async () => {
  if (!v2Result.value?.graph) {
    ElMessage.warning('请先完成知识抽取 V2')
    return
  }
  v2StoreLoading.value = true
  try {
    const { data } = await storeExtractionToNeo4j(v2Result.value.graph)
    ElMessage.success(`已存入 Neo4j：新建 ${data.nodes_created} 个节点，合并 ${data.nodes_merged} 个，创建 ${data.relations_created} 条关系`)
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '存储失败')
  } finally {
    v2StoreLoading.value = false
  }
}

const NEO4J_BROWSER_URL = 'http://localhost:7474/browser/'

const healthDetail = ref('')
const loadMainObjects = async () => {
  try {
    const { data } = await getUmlMainObjects()
    const items = Array.isArray(data?.items) ? data.items : []
    mainObjectOptions.value = items.map((it) => ({
      value: it.main_object,
      label: `${it.main_object}（${it.file_name}）`
    }))
    if (!form.main_object && items[0]?.main_object) {
      form.main_object = items[0].main_object
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '加载主对象列表失败')
  }
}

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
onMounted(loadMainObjects)
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
          <p><strong>主对象类型：</strong>从后端 `algorithm/uie/ie/UML` 目录自动解析并生成下拉选项。</p>
          <p><strong>使用流程：</strong></p>
          <ol>
            <li>选择主对象类型，输入待抽取文本</li>
            <li>点击「开始抽取」执行抽取，或「上传切块 JSON」对文件中每段文本分别抽取</li>
            <li>可上传 UML：直接新增主对象并生成新的 UML 文件，上传后自动解析并写入 Neo4j</li>
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

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>UML 上传</span>
        </template>
        <el-form :model="uploadForm" label-width="120px" class="extract-form">
          <el-form-item label="新增主对象" required>
            <el-input
              v-model="uploadForm.new_main_object"
              placeholder="输入主对象名称，例如：DeviceFault"
            />
          </el-form-item>
          <el-form-item>
            <div class="form-actions-row">
              <input
                ref="umlInputRef"
                type="file"
                accept=".puml,text/plain"
                class="hidden-file-input"
                @change="onUmlFile"
              >
              <el-button
                :loading="umlLoading"
                :icon="FolderOpened"
                type="primary"
                @click="pickUmlFile"
              >
                上传 UML 并解析
              </el-button>
            </div>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- 知识空间初始化 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>知识空间初始化</span>
        </template>
        <el-form label-width="120px" class="extract-form">
          <el-form-item label="维度-刻度映射" required>
            <el-input
              v-model="ksInput"
              type="textarea"
              :rows="4"
              placeholder="格式：维度:刻度1,刻度2
例如：
时间:年月
空间:集团,子公司
抽象层级:逻辑"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="ksLoading"
              @click="initKS"
            >
              初始化知识空间
            </el-button>
          </el-form-item>
        </el-form>
        <div v-if="ksResult" class="ks-result">
          <el-alert
            :type="ksResult.success ? 'success' : 'error'"
            :title="ksResult.message || ksResult.detail"
            :closable="false"
            show-icon
          />
          <p v-if="ksResult.name" class="ks-name">知识空间名称：<code>{{ ksResult.name }}</code></p>
        </div>
      </el-card>

      <!-- 维度刻度查询 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>维度刻度查询</span>
        </template>
        <el-form label-width="120px" class="extract-form">
          <el-form-item label="维度名称" required>
            <el-input
              v-model="dimQueryInput"
              placeholder="输入维度名称，例如：时间"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="dimQueryLoading"
              @click="queryDimScales"
            >
              查询刻度
            </el-button>
          </el-form-item>
        </el-form>
        <div v-if="dimQueryResult" class="dim-result">
          <h4>维度「{{ dimQueryResult.dimension }}」的刻度：</h4>
          <div v-if="dimQueryResult.scales?.length">
            <el-tag
              v-for="(s, idx) in dimQueryResult.scales"
              :key="idx"
              class="scale-tag"
              type="info"
            >
              {{ s.name }}
            </el-tag>
          </div>
          <p v-else class="dim-empty">该维度下暂无刻度</p>
        </div>
      </el-card>

      <!-- 知识抽取 V2 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>知识抽取 V2（本体驱动 · 无模板匹配）</span>
        </template>
        <el-form :model="v2Form" label-width="120px" class="extract-form">
          <el-form-item label="本体定义" required>
            <el-input
              v-model="v2Form.ontology"
              type="textarea"
              :rows="6"
              placeholder="粘贴 PUML 本体定义（PlantUML 格式 class 图）"
            />
          </el-form-item>
          <el-form-item label="待抽取文本" required>
            <el-input
              v-model="v2Form.text"
              type="textarea"
              :rows="4"
              placeholder="输入需要抽取知识的文本"
            />
          </el-form-item>
          <el-form-item label="模型（可选）">
            <el-input
              v-model="v2Form.model"
              placeholder="留空使用默认模型"
            />
          </el-form-item>
          <el-form-item label="API 地址（可选）">
            <el-input
              v-model="v2Form.base_url"
              placeholder="留空使用默认 API"
            />
          </el-form-item>
          <el-form-item>
            <div class="form-actions-row">
              <el-button
                type="primary"
                :loading="v2Loading"
                @click="extractV2"
              >
                开始抽取 V2
              </el-button>
              <el-button
                v-if="v2Result?.graph"
                type="success"
                :loading="v2StoreLoading"
                @click="storeV2ToNeo4j"
              >
                存入 Neo4j
              </el-button>
            </div>
          </el-form-item>
        </el-form>
        <!-- V2 提取结果 -->
        <div v-if="v2Result" class="v2-result">
          <el-tabs type="border-card">
            <el-tab-pane label="抽取原始 JSON (raw)">
              <pre class="result-json">{{ JSON.stringify(v2Result.raw, null, 2) }}</pre>
            </el-tab-pane>
            <el-tab-pane label="图结构 (graph)">
              <div class="graph-preview">
                <div v-if="v2Result.graph?.nodes?.length" class="graph-section">
                  <h4>节点 ({{ v2Result.graph.nodes.length }})</h4>
                  <el-table :data="v2Result.graph.nodes" size="small" max-height="240" border>
                    <el-table-column prop="uid" label="uid" width="120" show-overflow-tooltip />
                    <el-table-column prop="label" label="label" width="100" />
                    <el-table-column label="properties">
                      <template #default="{ row }">
                        <pre class="props-cell">{{ JSON.stringify(row.properties || {}) }}</pre>
                      </template>
                    </el-table-column>
                  </el-table>
                </div>
                <div v-if="v2Result.graph?.relationships?.length" class="graph-section">
                  <h4>关系 ({{ v2Result.graph.relationships.length }})</h4>
                  <el-table :data="v2Result.graph.relationships" size="small" max-height="200" border>
                    <el-table-column prop="from_uid" label="from" width="120" show-overflow-tooltip />
                    <el-table-column prop="to_uid" label="to" width="120" show-overflow-tooltip />
                    <el-table-column prop="type" label="type" />
                  </el-table>
                </div>
                <p v-if="!v2Result.graph?.nodes?.length && !v2Result.graph?.relationships?.length" class="graph-empty">
                  无图结构数据
                </p>
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>
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
              <p v-if="!result.graph?.nodes?.length && !result.graph?.relationships?.length" class="graph-empty">
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

/* 知识空间 */
.ks-result {
  margin-top: 12px;
}

.ks-name {
  margin-top: 8px;
  font-size: 14px;
  color: var(--gray-700);
}

.ks-name code {
  background: var(--gray-100);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

/* 维度刻度查询 */
.dim-result {
  margin-top: 12px;
}

.dim-result h4 {
  margin: 0 0 8px;
  font-size: 14px;
  color: var(--gray-800);
}

.scale-tag {
  margin-right: 8px;
  margin-bottom: 4px;
}

.dim-empty {
  color: var(--gray-500);
  font-size: 13px;
}

/* V2 抽取结果 */
.v2-result {
  margin-top: 16px;
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
