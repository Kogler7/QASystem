<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Delete, DataLine, Link } from '@element-plus/icons-vue'
import { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics } from '@/api'
import { ElMessage } from 'element-plus'

const loadingAdd = ref(false)
const loadingDelete = ref(false)
const loadingStats = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

// 添加：JSON 文本或默认示例
const addTriplesJson = ref(`[
  {"subject": "列车A", "predicate": "发生故障", "object": "制动系统", "confidence": 0.92},
  {"subject": "车站B", "predicate": "位于", "object": "北京", "confidence": 0.98}
]`)
const addResult = ref(null)

// 删除：JSON 文本
const deleteTriplesJson = ref(`[
  {"subject": "列车A", "predicate": "发生故障", "object": "制动系统"}
]`)
const deleteResult = ref(null)

// 统计
const statistics = ref(null)

const NEO4J_BROWSER_URL = 'http://localhost:7474/browser/'

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await kgUpdateHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '知识图谱更新模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端是否启动并配置 algorithm/KGUpdate/config.json'
  }
}

const parseTriplesJson = (str, needConfidence = false) => {
  let arr
  try {
    arr = JSON.parse(str)
  } catch (e) {
    throw new Error('JSON 格式错误：' + e.message)
  }
  if (!Array.isArray(arr)) throw new Error('请输入数组格式，例如 [{"subject":"...","predicate":"...","object":"..."}]')
  const required = needConfidence ? ['subject', 'predicate', 'object', 'confidence'] : ['subject', 'predicate', 'object']
  for (let i = 0; i < arr.length; i++) {
    const t = arr[i]
    for (const k of required) {
      if (t[k] === undefined || t[k] === null || String(t[k]).trim() === '') {
        throw new Error(`第 ${i + 1} 条缺少或为空: ${k}`)
      }
    }
    if (needConfidence) {
      const c = Number(t.confidence)
      if (Number.isNaN(c) || c < 0 || c > 1) throw new Error(`第 ${i + 1} 条 confidence 须为 0~1 之间数字`)
    }
  }
  return arr
}

const submitAdd = async () => {
  let triples
  try {
    triples = parseTriplesJson(addTriplesJson.value, true)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingAdd.value = true
  addResult.value = null
  try {
    const { data } = await kgUpdateAdd(triples)
    addResult.value = data
    ElMessage.success(data?.message || '添加完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '添加失败')
    addResult.value = null
  } finally {
    loadingAdd.value = false
  }
}

const submitDelete = async () => {
  let triples
  try {
    triples = parseTriplesJson(deleteTriplesJson.value, false)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingDelete.value = true
  deleteResult.value = null
  try {
    const { data } = await kgUpdateDelete(triples)
    deleteResult.value = data
    ElMessage.success(data?.message || '删除完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '删除失败')
    deleteResult.value = null
  } finally {
    loadingDelete.value = false
  }
}

const fetchStatistics = async () => {
  loadingStats.value = true
  statistics.value = null
  try {
    const { data } = await kgUpdateStatistics()
    statistics.value = data
    ElMessage.success('统计已刷新')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '获取统计失败')
    statistics.value = null
  } finally {
    loadingStats.value = false
  }
}

function openNeo4jBrowser() {
  window.open(NEO4J_BROWSER_URL, '_blank')
}

onMounted(checkHealth)
</script>

<template>
  <div class="kg-update-view">
    <div class="kg-update-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="kg-update-inner">
      <h1 class="page-title">
        <span class="title-highlight">知识图谱更新</span>
      </h1>
      <p class="page-desc">
        批量添加或删除 Neo4j 三元组，支持置信度阈值下的新增/更新/拒绝逻辑；可查看图谱统计。
      </p>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">更新服务：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
        <el-button link type="primary" size="small" :icon="Link" @click="openNeo4jBrowser">查看图谱</el-button>
      </div>

      <!-- 统计 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>图谱统计</span>
          <el-button type="primary" link :loading="loadingStats" :icon="DataLine" @click="fetchStatistics">刷新</el-button>
        </template>
        <div v-if="statistics?.success && statistics?.statistics" class="stats-box">
          <p>节点数：<strong>{{ statistics.statistics.node_count }}</strong></p>
          <p>关系数：<strong>{{ statistics.statistics.relationship_count }}</strong></p>
          <p v-if="statistics.statistics.confidence_distribution" class="confidence-row">
            置信度分布（有 confidence 的关系）：
            高(≥0.85) <strong>{{ statistics.statistics.confidence_distribution.high }}</strong>，
            中(0.7~&lt;0.85) <strong>{{ statistics.statistics.confidence_distribution.medium }}</strong>，
            低(&lt;0.7) <strong>{{ statistics.statistics.confidence_distribution.low }}</strong>
            <template v-if="statistics.statistics.confidence_distribution.total_with_confidence != null">
              ，合计 <strong>{{ statistics.statistics.confidence_distribution.total_with_confidence }}</strong>
            </template>
          </p>
        </div>
        <p v-else-if="statistics && !statistics.success" class="stats-error">{{ statistics.error || '获取失败' }}</p>
        <p v-else class="stats-placeholder">点击「刷新」获取 Neo4j 图谱统计</p>
      </el-card>

      <!-- 添加三元组 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>添加三元组</span>
        </template>
        <p class="form-hint-block">每行一条 JSON，需包含 subject、predicate、object、confidence（0~1）。</p>
        <el-input
          v-model="addTriplesJson"
          type="textarea"
          :rows="8"
          placeholder='[{"subject":"...","predicate":"...","object":"...","confidence":0.9}]'
          class="triples-textarea"
        />
        <el-button type="primary" :loading="loadingAdd" :icon="Plus" @click="submitAdd" style="margin-top: 12px">
          执行添加
        </el-button>
        <div v-if="addResult" class="result-box">
          <p class="result-summary">{{ addResult.message }}</p>
          <pre v-if="addResult.statistics" class="result-json">{{ JSON.stringify(addResult.statistics, null, 2) }}</pre>
          <el-collapse v-if="addResult.details?.length">
            <el-collapse-item title="查看每条处理详情" name="add-details">
              <pre class="result-details">{{ JSON.stringify(addResult.details, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>
      </el-card>

      <!-- 删除三元组 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>删除三元组</span>
        </template>
        <p class="form-hint-block">每条 JSON 需包含 subject、predicate、object。</p>
        <el-input
          v-model="deleteTriplesJson"
          type="textarea"
          :rows="5"
          placeholder='[{"subject":"...","predicate":"...","object":"..."}]'
          class="triples-textarea"
        />
        <el-button type="danger" :loading="loadingDelete" :icon="Delete" @click="submitDelete" style="margin-top: 12px">
          执行删除
        </el-button>
        <div v-if="deleteResult" class="result-box">
          <p class="result-summary">{{ deleteResult.message }}</p>
          <pre v-if="deleteResult.statistics" class="result-json">{{ JSON.stringify(deleteResult.statistics, null, 2) }}</pre>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.kg-update-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.kg-update-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.kg-update-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.kg-update-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.kg-update-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.kg-update-inner {
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
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
}

.form-hint-block {
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 8px;
}

.triples-textarea {
  font-family: ui-monospace, monospace;
  font-size: 13px;
}

.result-box {
  margin-top: 16px;
  padding: 12px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-summary {
  font-weight: 500;
  margin-bottom: 8px;
  color: var(--gray-800);
}
.result-json, .result-details {
  font-size: 12px;
  overflow: auto;
  max-height: 240px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.stats-box p, .stats-placeholder, .stats-error {
  margin: 4px 0;
  font-size: 14px;
  color: var(--gray-700);
}
.stats-error {
  color: var(--el-color-danger);
}

@media (max-width: 768px) {
  .kg-update-view {
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
