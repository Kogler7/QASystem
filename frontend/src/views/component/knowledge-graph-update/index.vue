<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Delete, DataLine, Link } from '@element-plus/icons-vue'
import { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics, kgCalculateConfidence } from '@/api'
import { ElMessage } from 'element-plus'

const loadingAdd = ref(false)
const loadingDelete = ref(false)
const loadingStats = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

// 添加：JSON 文本或默认示例（无置信度）
const addTriplesJson = ref(`[
  {"subject": "一等客车", "predicate": "定义", "object": "定员少、乘坐舒适度高的客车"},
  {"subject": "宿营车", "predicate": "定义", "object": "供乘务或作业人员休息使用的车辆"}
]`)
const calculatedTriplesJson = ref('')  // 新增：计算后的带置信度 JSON
const addResult = ref(null)

// 删除：JSON 文本
const deleteTriplesJson = ref(`[
  {"subject": "一等客车", "predicate": "定义", "object": "定员少、乘坐舒适度高的客车"}
]`)
const deleteResult = ref(null)

// 统计
const statistics = ref(null)

const NEO4J_BROWSER_URL = 'http://localhost:7474/browser/'

// 新增：侧边栏活动项
const activeMenu = ref('confidence')

// 新增：进度条状态
const currentStep = ref(0)
const resetSteps = () => { currentStep.value = 0 }
const updateStep = (i) => { currentStep.value = i }

// 新增：计算置信度加载状态
const loadingCalculate = ref(false)

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

// 新增：计算置信度
const submitCalculate = async () => {
  resetSteps()
  updateStep(1) // 输入
  let triples
  try {
    triples = parseTriplesJson(addTriplesJson.value, false)
    updateStep(2) // 文本搜索
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingCalculate.value = true
  calculatedTriplesJson.value = ''
  try {
    updateStep(3) // 路径搜索
    const { data } = await kgCalculateConfidence(triples)  // 调用算法，返回带置信度的三元组
    updateStep(4) // 嵌入计算
    updateStep(5) // 置信度计算
    calculatedTriplesJson.value = JSON.stringify(data.triples, null, 2)  // 假设返回 { triples: [...] }
    ElMessage.success('置信度计算完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '计算失败')
  } finally {
    loadingCalculate.value = false
  }
}

const submitAdd = async () => {
  if (!calculatedTriplesJson.value) {
    ElMessage.warning('请先计算置信度')
    return
  }
  let triples
  try {
    triples = parseTriplesJson(calculatedTriplesJson.value, true)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingAdd.value = true
  addResult.value = null
  try {
    const { data } = await kgUpdateAdd(triples)
    updateStep(6) // 更新完成
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
  resetSteps()
  updateStep(1) // 输入
  let triples
  try {
    triples = parseTriplesJson(deleteTriplesJson.value, false)
    updateStep(2)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingDelete.value = true
  deleteResult.value = null
  try {
    updateStep(3)
    const { data } = await kgUpdateDelete(triples)
    updateStep(5)
    deleteResult.value = data
    ElMessage.success(data?.message || '删除完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '删除失败')
    deleteResult.value = null
  } finally {
    loadingDelete.value = false
    updateStep(6)
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
        批量添加或删除 Neo4j 三元组；可查看图谱统计。
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

      <!-- 新增：侧边栏布局 -->
      <div class="content-layout">
        <el-menu :default-active="activeMenu" class="sidebar-menu" @select="(key) => activeMenu = key">
          <el-menu-item index="confidence">
            <span>置信度计算与更新</span>
          </el-menu-item>
          <el-menu-item index="conflict">
            <span>冲突检测与修复</span>
          </el-menu-item>
        </el-menu>
        <div class="main-content">
          <!-- 置信度计算与更新内容 -->
          <div v-if="activeMenu === 'confidence'">
            <!-- 新增：算法介绍卡片 -->
            <el-card class="algorithm-intro-card" shadow="hover">
              <template #header>
                <div class="card-header">
                  <span>🧠 置信度算法：动态混合模型 (DynamicHybrid)</span>
                </div>
              </template>
              <div class="algorithm-content">
                <p class="algorithm-desc">
                  本系统采用 <strong>DynamicHybrid 动态混合模型</strong> 计算三元组置信度，融合三大证据源：
                </p>
                <div class="evidence-grid">
                  <div class="evidence-item">
                    <span class="evidence-icon text">📄</span>
                    <div class="evidence-info">
                      <span class="evidence-name">文本证据</span>
                      <span class="evidence-detail">从语料中提取的语义一致性 (128维)</span>
                    </div>
                  </div>
                  <div class="evidence-item">
                    <span class="evidence-icon kg">🔗</span>
                    <div class="evidence-info">
                      <span class="evidence-name">知识图谱嵌入</span>
                      <span class="evidence-detail">实体关系的嵌入表示 (128维)</span>
                    </div>
                  </div>
                  <div class="evidence-item">
                    <span class="evidence-icon path">🛤️</span>
                    <div class="evidence-info">
                      <span class="evidence-name">路径证据</span>
                      <span class="evidence-detail">多跳推理路径的聚合得分 (64维 + 最优路径)</span>
                    </div>
                  </div>
                </div>
                <div class="algorithm-footer">
                  <p class="fusion-info">
                    <span class="badge">融合层</span> 三大特征 + 各组件得分 → 全连接网络 → 最终置信度 (0~1)
                  </p>
                </div>
              </div>
            </el-card>

            <!-- 新增：进度条 -->
            <el-steps :active="currentStep" finish-status="success" align-center class="process-steps">
              <el-step title="输入"></el-step>
              <el-step title="文本搜索"></el-step>
              <el-step title="路径搜索"></el-step>
              <el-step title="嵌入计算"></el-step>
              <el-step title="置信度计算"></el-step>
              <el-step title="更新完成"></el-step>
            </el-steps>

            <!-- 统计 -->
            <el-card class="form-card" shadow="hover">
              <template #header>
                <span>图谱统计</span>
                <el-button type="primary" link :loading="loadingStats" :icon="DataLine" @click="fetchStatistics">刷新</el-button>
              </template>
              <div v-if="statistics?.success && statistics?.statistics" class="stats-box">
                <p>节点数：<strong>{{ statistics.statistics.node_count }}</strong></p>
                <p>关系数：<strong>{{ statistics.statistics.relationship_count }}</strong></p>
              </div>
              <p v-else-if="statistics && !statistics.success" class="stats-error">{{ statistics.error || '获取失败' }}</p>
              <p v-else class="stats-placeholder">点击「刷新」获取 Neo4j 图谱统计</p>
            </el-card>

            <!-- 添加三元组 -->
            <el-card class="form-card" shadow="hover">
              <template #header>
                <span>添加三元组</span>
              </template>
              <el-row :gutter="20">
                <el-col :span="12">
                  <p class="form-hint-block">输入三元组（无置信度），每行一条 JSON。</p>
                  <el-input
                    v-model="addTriplesJson"
                    type="textarea"
                    :rows="8"
                    placeholder='[{"subject":"...","predicate":"...","object":"..."}]'
                    class="triples-textarea"
                  />
                  <el-button type="primary" :loading="loadingCalculate" @click="submitCalculate" style="margin-top: 12px">
                    计算置信度
                  </el-button>
                </el-col>
                <el-col :span="12">
                  <p class="form-hint-block">带置信度的三元组（计算后显示）。</p>
                  <el-input
                    v-model="calculatedTriplesJson"
                    type="textarea"
                    :rows="8"
                    readonly
                    placeholder="计算后显示带置信度的 JSON"
                    class="triples-textarea"
                  />
                  <el-button type="primary" :loading="loadingAdd" :disabled="!calculatedTriplesJson" @click="submitAdd" style="margin-top: 12px">
                    执行添加
                  </el-button>
                </el-col>
              </el-row>
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
          <!-- 冲突检测与修复内容（空页面） -->
          <div v-else-if="activeMenu === 'conflict'">
            <el-card class="form-card" shadow="hover">
              <template #header>
                <span>冲突检测与修复</span>
              </template>
              <p>此功能正在开发中。</p>
            </el-card>
          </div>
        </div>
      </div>
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

.content-layout {
  display: flex;
  gap: 24px;
}
.sidebar-menu {
  width: 200px;
  flex-shrink: 0;
}
.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.algorithm-intro-card {
  margin-bottom: 8px;
  border-left: 4px solid var(--primary-color);
  
  :deep(.el-card__header) {
    padding: 12px 20px;
    background: var(--gray-50);
    border-bottom: 1px solid var(--gray-200);
  }
  
  .card-header {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.algorithm-content {
  padding: 8px 0;
  
  .algorithm-desc {
    margin-bottom: 16px;
    color: var(--gray-700);
    font-size: 14px;
  }
}

.evidence-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 20px;
  
  @media (max-width: 768px) {
    grid-template-columns: 1fr;
  }
}

.evidence-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px;
  background: var(--gray-50);
  border-radius: 8px;
  
  .evidence-icon {
    font-size: 24px;
    line-height: 1;
    
    &.text { color: #409EFF; }
    &.kg { color: #67C23A; }
    &.path { color: #E6A23C; }
  }
  
  .evidence-info {
    display: flex;
    flex-direction: column;
    
    .evidence-name {
      font-weight: 600;
      font-size: 15px;
      color: var(--gray-900);
      margin-bottom: 4px;
    }
    
    .evidence-detail {
      font-size: 12px;
      color: var(--gray-600);
    }
  }
}

.algorithm-footer {
  margin-top: 8px;
  padding-top: 16px;
  border-top: 1px dashed var(--gray-300);
  
  .fusion-info {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--gray-700);
    margin: 0;
  }
  
  .badge {
    display: inline-block;
    padding: 2px 8px;
    background: var(--primary-color);
    color: white;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
  }
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
  .content-layout {
    flex-direction: column;
  }
  .sidebar-menu {
    width: 100%;
  }
}
</style>