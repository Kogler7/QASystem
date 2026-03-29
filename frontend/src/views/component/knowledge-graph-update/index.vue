<script setup>
import { ref, reactive, onMounted, watch, nextTick, onUnmounted } from 'vue'
import { Plus, Delete, DataLine, Link, Warning, MagicStick, Upload, Document } from '@element-plus/icons-vue'
import { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics, kgCalculateConfidence, processSchemaOutput, addFromComputed } from '@/api'
import { ElMessage } from 'element-plus'
import axios from 'axios'
import { Network } from 'vis-network'

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
const calculatedTriplesJson = ref('')
const addResult = ref(null)

// 删除：JSON 文本
const deleteTriplesJson = ref(`[
  {"subject": "一等客车", "predicate": "定义", "object": "定员少、乘坐舒适度高的客车"}
]`)
const deleteResult = ref(null)

// 统计
const statistics = ref(null)

const NEO4J_BROWSER_URL = 'http://localhost:7474/browser/'

// 侧边栏活动项
const activeMenu = ref('confidence')

// 置信度流程：进度条状态
const currentStep = ref(0)
const resetSteps = () => { currentStep.value = 0 }
const updateStep = (i) => { currentStep.value = i }

// 计算置信度加载状态
const loadingCalculate = ref(false)

// 知识抽取结果处理相关变量
const schemaJsonInput = ref('')
const extractedTriplesJson = ref('')
const loadingProcess = ref(false)
const processResult = ref(null)
const computedFullRelations = ref([])
const computedPredictions = ref([])
const computedHighRelations = ref([])

// 冲突检测与修复
const turtleInput = ref('')
const conflictGroups = ref([])
const activeGroupIndex = ref(null)
const loadingDetect = ref(false)
const loadingRepair = ref(false)
const visContainer = ref(null)
let networkInstance = null
const conflictStep = ref(0)
// vis-network instance (destroyed/recreated when rendering the graph)

// 健康检查
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

// 解析 JSON
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

// 计算简单三元组的置信度
const submitCalculate = async () => {
  resetSteps()
  updateStep(1)
  let triples
  try {
    triples = parseTriplesJson(addTriplesJson.value, false)
    updateStep(2)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  loadingCalculate.value = true
  calculatedTriplesJson.value = ''
  try {
    updateStep(3)
    const { data } = await kgCalculateConfidence(triples)
    updateStep(4)
    updateStep(5)
    calculatedTriplesJson.value = JSON.stringify(data.triples, null, 2)
    ElMessage.success('置信度计算完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '计算失败')
  } finally {
    loadingCalculate.value = false
  }
}

// 执行简单三元组添加
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
    updateStep(6)
    addResult.value = data
    ElMessage.success(data?.message || '添加完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '添加失败')
    addResult.value = null
  } finally {
    loadingAdd.value = false
  }
}

// 删除三元组
const submitDelete = async () => {
  resetSteps()
  updateStep(1)
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

// 获取图谱统计
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

// ========== 知识抽取结果处理 ==========
// 解析并计算置信度（不入库）
const processSchemaOutputOnly = async () => {
  if (!schemaJsonInput.value.trim()) {
    ElMessage.warning('请输入 JSON 数据')
    return
  }
  
  let data
  try {
    data = JSON.parse(schemaJsonInput.value)
  } catch (e) {
    ElMessage.error('JSON 格式错误: ' + e.message)
    return
  }
  
  loadingProcess.value = true
  processResult.value = null
  computedFullRelations.value = []
  computedPredictions.value = []
  computedHighRelations.value = []
  
  try {
    const response = await processSchemaOutput(data, 0.7)
    const result = response.data
    
    const allRelations = [...(result.relations_high || []), ...(result.relations_low || [])]
    extractedTriplesJson.value = JSON.stringify(allRelations, null, 2)
    
    computedFullRelations.value = result.full_relations || []
    computedPredictions.value = result.predictions || []
    computedHighRelations.value = result.relations_high || []
    
    processResult.value = result
    
    if (result.success) {
      ElMessage.success(result.message)
    } else {
      ElMessage.warning(result.message)
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '处理失败')
  } finally {
    loadingProcess.value = false
  }
}

// 批量入库（从计算结果入库，保留完整属性）
const submitAddExtracted = async () => {
  if (computedHighRelations.value.length === 0) {
    ElMessage.warning('没有高置信度的关系需要入库')
    return
  }
  
  loadingAdd.value = true
  
  try {
    const response = await addFromComputed(
      computedHighRelations.value,
      computedFullRelations.value,
      computedPredictions.value,
      0.7
    )
    const data = response.data
    
    ElMessage.success(data?.message || '入库完成')
    fetchStatistics()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '入库失败')
  } finally {
    loadingAdd.value = false
  }
}

// 清空 schema 输入
const clearSchemaInput = () => {
  schemaJsonInput.value = ''
  extractedTriplesJson.value = ''
  processResult.value = null
  computedFullRelations.value = []
  computedPredictions.value = []
  computedHighRelations.value = []
}

// ========== 冲突检测与修复 ==========
const handleFileUpload = (file) => {
  const reader = new FileReader()
  reader.onload = (e) => {
    turtleInput.value = e.target.result
    ElMessage.success('文件加载成功')
  }
  reader.readAsText(file.raw)
}

const handleConflictDetect = async () => {
  if (!turtleInput.value.trim()) return ElMessage.warning('请先输入 Turtle 数据')
  loadingDetect.value = true
  conflictGroups.value = []
  conflictStep.value = 1
  try {
    const { data } = await axios.post('/api/conflict/detect', {
      insert_ttl: turtleInput.value,
      delete_ttl: ''
    })
    if (data.success) {
      const raw = data.conflicts || [];
        conflictGroups.value = raw.map((c) => ({
          ...c,
          // 🚨 核心改动：显示的时候，直接脱壳！
          subject: cleanId(c.subject), 
          predicate: cleanId(c.predicate),
          selectedResolution: c.selectedResolution || 'overwrite',
          customValue: c.customValue || ''
        }));
      if (conflictGroups.value.length > 0) {
        const msg = data.message || `冲突检测完成：发现 ${conflictGroups.value.length} 项违规数据。`
        activeGroupIndex.value = 0
        conflictStep.value = 2
        ElMessage({ message: msg, type: 'warning', duration: 5000, showClose: true })
      } else {
        activeGroupIndex.value = null
        conflictStep.value = 5
        ElMessage.success('冲突检测完成：未发现任何冲突，数据已自动同步！')
      }
    }
  } catch (err) {
    console.error('检测接口报错:', err)
    conflictStep.value = 0
    ElMessage.error(err?.response?.data?.detail || '检测失败，请检查后端服务')
  } finally {
    loadingDetect.value = false
  }
}

const handleSingleRepair = async () => {
  if (activeGroupIndex.value === null) return
  const current = conflictGroups.value[activeGroupIndex.value]
  loadingRepair.value = true
  conflictStep.value = 3
  try {
    const payload = {
      actions: [{
        subject: current.subject,
        predicate: current.predicate,
        operation: current.operation || 'add',
        action_type: current.selectedResolution,
        value: current.selectedResolution === 'manual' ? current.customValue : current.newValue
      }]
    }
    const { data } = await axios.post('/api/conflict/repair', payload)
    if (data.success) {
      ElMessage.success(`[${current.subject}] 修复成功，已同步至数据库`)
      conflictGroups.value.splice(activeGroupIndex.value, 1)
      if (conflictGroups.value.length > 0) {
        activeGroupIndex.value = 0
        conflictStep.value = 2
      } else {
        activeGroupIndex.value = null
        conflictStep.value = 5
      }
    }
  } catch (err) {
    console.error('修复接口报错:', err)
    conflictStep.value = 2
    ElMessage.error(err?.response?.data?.detail || '修复执行失败')
  } finally {
    loadingRepair.value = false
  }
}
const cleanId = (str) => {
  if (!str) return '';
  // 过滤掉尖括号，取最后一段
  return str.replace(/[<>]/g, '').split('/').pop();
};
watch(conflictStep, async (newVal) => {
  if (newVal === 2 || newVal === 3) {
    await nextTick()
    renderConflictGraph()
  }
})

const renderConflictGraph = () => {
  if (!visContainer.value) return
  if (networkInstance) {
    networkInstance.destroy()
    networkInstance = null
  }
  const nodes = []
  const edges = []
  
  // 🚨 关键修复 1：用一个 Set 记录已经画过的主语，防止红点重复画导致崩溃
  const addedSubjects = new Set()

  // 🚨 关键修复 2：加上 index 参数，给后续的连线和方块加上唯一编号
  conflictGroups.value.forEach((conflict, index) => {
    
    // 1. 画主语节点（中心红点）
    if (!addedSubjects.has(conflict.subject)) {
      nodes.push({
        id: conflict.subject, // 这里不用加 index，因为它是中心点
        label: `${conflict.subject}\n(违规主体)`,
        color: { background: '#fef0f0', border: '#f56c6c' },
        shape: 'dot',
        size: 35,
        font: { multi: 'md', color: '#f56c6c', weight: 'bold' }
      })
      addedSubjects.add(conflict.subject)
    }

    // 2. 画旧值节点（绿色方块）
    if (conflict.oldValue !== '无记录') {
      // 🚨 关键修复 3：ID 加上 _${index}，确保绝对唯一
      const oldNodeId = `old_${conflict.subject}_${index}`
      nodes.push({
        id: oldNodeId,
        label: `库内原值:\n${conflict.oldValue}`,
        color: { background: '#f0f9eb', border: '#67c23a' },
        shape: 'box',
        size: 20
      })
      // 💡 顺便优化：连线上显示具体的属性名（比如 speed），图表更好看
      const edgeLabel = conflict.predicate ? conflict.predicate : '当前属性'
      edges.push({ from: conflict.subject, to: oldNodeId, label: edgeLabel, color: '#67c23a' })
    }

    // 3. 画新值节点（橙色方块）
    const newNodeId = `new_${conflict.subject}_${index}` // 🚨 同理加 index
    nodes.push({
      id: newNodeId,
      label: `拟输入值:\n${conflict.newValue}`,
      color: { background: '#fdf6ec', border: '#e6a23c' },
      shape: 'box',
      size: 20
    })
    const newEdgeLabel = conflict.predicate ? `${conflict.predicate} 冲突` : '冲突更新'
    edges.push({ from: conflict.subject, to: newNodeId, label: newEdgeLabel, dashes: true, color: '#e6a23c' })
  })

  // --- 下面是实例化和点击事件 ---
  const options = {
    physics: {
      enabled: true,
      barnesHut: { gravitationalConstant: -3000, springLength: 150 }
    },
    interaction: { hover: true, tooltipDelay: 200 }
  }
  
  networkInstance = new Network(visContainer.value, { nodes, edges }, options)
  
  // 🚨 关键修复 4：因为前面 ID 变了，这里的点击匹配逻辑也要微微调整
  networkInstance.on('click', (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0]
      // 用正则把前缀 old_ / new_ 和后缀 _0 去掉，还原出真正的 subjectId
      let subjectId = nodeId
      if (nodeId.startsWith('old_') || nodeId.startsWith('new_')) {
         subjectId = nodeId.replace(/^(old|new)_/, '').replace(/_\d+$/, '')
      }
      
      const index = conflictGroups.value.findIndex((c) => c.subject === subjectId)
      if (index !== -1) activeGroupIndex.value = index
    }
  })
}

const handleApplyRepair = async () => {
  const index = activeGroupIndex.value
  if (index === null || !conflictGroups.value[index]) return
  const current = conflictGroups.value[index]
  const resolution = current.selectedResolution
  let finalValue = ''
  if (resolution === 'manual') {
    if (!current.customValue) return ElMessage.warning('请输入人工修正值')
    finalValue = current.customValue
  } else if (resolution === 'overwrite') {
    finalValue = current.newValue
  } else if (resolution === 'keep') {
    return handleFinishOrNext()
  }
  const repairAction = {
    subject: current.subject.replace(/^ex:/, ''),
    predicate: current.predicate.replace(/^ex:/, ''),
    value: finalValue,
    operation: 'add'
  }
  try {
    const { data } = await axios.post('/api/conflict/repair', { actions: [repairAction] })
    if (data.success) {
      ElMessage.success(`修正成功: ${finalValue}`)
      handleFinishOrNext()
    }
  } catch (err) {
    console.error('修复失败:', err)
    ElMessage.error(err?.response?.data?.detail || '数据库写入异常')
  }
}

const proceedToNext = () => {
  if (activeGroupIndex.value < conflictGroups.value.length - 1) {
    activeGroupIndex.value += 1
  }
}

const handleFinishOrNext = () => {
  if (activeGroupIndex.value === conflictGroups.value.length - 1) {
    conflictStep.value = 5
    ElMessage.success('所有冲突已处理完毕！')
  } else {
    proceedToNext()
  }
}

const resetConflictFlow = () => {
  turtleInput.value = ''
  conflictStep.value = 0
  conflictGroups.value = []
  activeGroupIndex.value = null
}

const clearConflictInput = () => {
  turtleInput.value = ''
  conflictStep.value = 0
}

const onConflictInput = () => {
  conflictStep.value = 0
}

onMounted(() => {
  checkHealth()
})
onUnmounted(() => {
  if (networkInstance) networkInstance.destroy()
})
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

      <!-- 侧边栏布局 -->
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
            <!-- 算法介绍卡片 -->
            <el-card class="algorithm-intro-card" shadow="hover">
              <template #header>
                <div class="card-header">
                  <span>置信度算法：动态混合模型 (DynamicHybrid)</span>
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

            <!-- 进度条 -->
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

            <!-- 知识抽取结果处理（新增） -->
            <el-card class="form-card" shadow="hover">
              <template #header>
                <div class="card-header-flex">
                  <span>知识抽取结果处理</span>
                  <el-button type="primary" link :icon="Document" @click="clearSchemaInput">清空</el-button>
                </div>
              </template>
              <el-row :gutter="20">
                <el-col :span="12">
                  <p class="form-hint-block">
                    输入 schema_mapper 输出的 JSON（包含 raw 和 graph）
                  </p>
                  <el-input
                    v-model="schemaJsonInput"
                    type="textarea"
                    :rows="12"
                    placeholder='{
  "raw": {
    "Term": [...]
  },
  "graph": {
    "nodes": [...],
    "relationships": [...]
  }
}'
                    class="json-textarea"
                  />
                  <el-button 
                    type="primary" 
                    :loading="loadingProcess"
                    @click="processSchemaOutputOnly" 
                    style="margin-top: 12px"
                  >
                    解析并计算置信度
                  </el-button>
                </el-col>
                <el-col :span="12">
                  <p class="form-hint-block">
                    提取的关系及置信度（带置信度的三元组）
                  </p>
                  <el-input
                    v-model="extractedTriplesJson"
                    type="textarea"
                    :rows="12"
                    readonly
                    placeholder="解析后将显示提取的关系及置信度"
                    class="json-textarea"
                  />
                  <div class="action-buttons">
                    <el-button 
                      type="success" 
                      :loading="loadingAdd"
                      :disabled="computedHighRelations.length === 0"
                      @click="submitAddExtracted"
                      style="margin-top: 12px; margin-right: 12px"
                    >
                      批量入库（置信度≥阈值）
                    </el-button>
                    <el-tooltip content="当前阈值可在后端配置文件中修改" placement="top">
                      <el-tag type="info" style="margin-top: 12px">阈值: 0.7</el-tag>
                    </el-tooltip>
                  </div>
                  <div v-if="processResult?.statistics" class="stat-summary" style="margin-top: 12px">
                    <el-tag type="success" size="small">高置信度: {{ processResult.statistics.high }}</el-tag>
                    <el-tag type="danger" size="small" style="margin-left: 8px">低置信度: {{ processResult.statistics.low }}</el-tag>
                  </div>
                </el-col>
              </el-row>
              <div v-if="processResult" class="result-box">
                <p class="result-summary">{{ processResult.message }}</p>
                <el-collapse>
                  <el-collapse-item title="查看高置信度关系详情" name="high-details">
                    <pre class="result-details">{{ JSON.stringify(processResult.relations_high, null, 2) }}</pre>
                  </el-collapse-item>
                  <el-collapse-item v-if="processResult.relations_low?.length" title="查看低置信度关系详情" name="low-details">
                    <pre class="result-details">{{ JSON.stringify(processResult.relations_low, null, 2) }}</pre>
                  </el-collapse-item>
                </el-collapse>
              </div>
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

          <!-- 冲突检测与修复内容 -->
          <div v-else-if="activeMenu === 'conflict'" class="conflict-split-view">
            <el-card shadow="never" style="margin-bottom: 20px; border: none; background: transparent;">

              <el-card class="algorithm-annotation-card" shadow="never">
                <div class="card-header-wrapper">
                  <span class="header-icon">🧠</span>
                  <span class="header-title">冲突推演算法：基于 SHACL 的串行逻辑推演模型</span>
                </div>

                <p class="main-description">
                  本系统采用“先约束过滤、后逻辑求解”的漏斗式架构定位图谱冲突，融合三大核心步骤：
                </p>

                <el-row :gutter="40" class="features-row">
                  <el-col :span="8">
                    <div class="feature-block">
                      <div class="feature-icon">
                      </div>
                      <div class="feature-text">
                        <div class="feature-title">查找最小冲突子图</div>
                        <div class="feature-desc">利用约束规则，从全量图谱中提取最小候选子图</div>
                      </div>
                    </div>
                  </el-col>

                  <el-col :span="8">
                    <div class="feature-block">
                      <div class="feature-icon">
                      </div>
                      <div class="feature-text">
                        <div class="feature-title">Clingo 精准求解</div>
                        <div class="feature-desc">在剪枝后的子图空间内，根据约束规则计算冲突根因</div>
                      </div>
                    </div>
                  </el-col>

                  <el-col :span="8">
                    <div class="feature-block">
                      <div class="feature-icon">
                      </div>
                      <div class="feature-text">
                        <div class="feature-title">修复三元组</div>
                        <div class="feature-desc">根据逻辑推演结果，选择最优方案</div>
                      </div>
                    </div>
                  </el-col>
                </el-row>
              </el-card>
              <el-steps :active="conflictStep" finish-status="success" align-center>
                <el-step title="数据输入" />
                <el-step title="冲突检测" />
                <el-step title="展示与修复" />
                <el-step title="正在修复" />
                <el-step title="更新完成" />
              </el-steps>
            </el-card>

            <div class="conflict-wrapper">
              <el-card class="form-card" shadow="hover" v-show="conflictStep < 2">
                <template #header>
                  <div class="card-header-flex">
                    <span>RDF Turtle 或 JSON 数据输入</span>
                    <el-upload action="#" :auto-upload="false" :show-file-list="false" @change="handleFileUpload">
                      <el-button type="primary" link :icon="Link">请输入 Turtle 或 JSON 数据</el-button>
                    </el-upload>
                  </div>
                </template>
                <el-input
                  v-model="turtleInput"
                  type="textarea"
                  :rows="8"
                  placeholder="@prefix ex: <http://example.org/> ... "
                  class="turtle-editor"
                  @input="onConflictInput"
                />
                <div style="margin-top: 12px; display: flex; gap: 10px;">
                  <el-button type="primary" :loading="loadingDetect" @click="handleConflictDetect">
                    开始冲突检测
                  </el-button>
                  <el-button @click="clearConflictInput">清空</el-button>
                </div>
              </el-card>

              <el-card class="graph-card" shadow="hover" v-show="conflictStep === 2 || conflictStep === 3" style="margin-bottom: 20px;">
                <template #header>
                  <div class="card-header-flex">
                    <span style="font-weight: bold; color: #f56c6c;">
                      <el-icon><Link /></el-icon> 最小冲突子图动态展示
                    </span>
                    <el-button type="primary" link @click="renderConflictGraph">重置布局</el-button>
                  </div>
                </template>
                <div ref="visContainer" style="height: 350px; background: #ffffff; border-radius: 8px;"></div>
              </el-card>

              <el-row :gutter="20" v-show="(conflictStep === 2 || conflictStep === 3) && conflictGroups.length > 0" style="margin-top: 20px;">
                <el-col :span="12">
                  <h3 class="sub-title"><el-icon><Warning /></el-icon> 发现冲突项</h3>
                  <div class="scroll-container">
                    <el-card
                      v-for="(group, index) in conflictGroups"
                      :key="index"
                      class="conflict-item-card"
                      :class="{ 'is-active': activeGroupIndex === index }"
                      @click="activeGroupIndex = index">
                      <div class="conflict-header">
                        <el-tag type="danger" size="small">{{ group.conflictType }}</el-tag>
                        <span class="subject-text">{{ group.subjectName || group.subject }}</span>
                      </div>
                      <p class="predicate-text">
                        <span v-if="group.conflictType && group.conflictType.includes('关系')">关系：</span>
                        <span v-else-if="group.conflictType && group.conflictType.includes('标签')">标签：</span>
                        <span v-else>属性：</span>
                        
                        <code>{{ group.predicateName || group.predicate }}</code>
                      </p>
                      <div class="comparison-grid">
                        <div class="val-box old">
                          <span class="label">数据库现有:</span>
                          <span class="value">{{ group.oldValue }}</span>
                        </div>
                        <div class="val-box new">
                          <span class="label">本次输入:</span>
                          <span class="value">{{ group.newValue }}</span>
                        </div>
                      </div>
                    </el-card>
                  </div>
                </el-col>

                <el-col :span="12">
                  <h3 class="sub-title"><el-icon><MagicStick /></el-icon> 修复建议</h3>
                  <el-card v-if="activeGroupIndex !== null" shadow="never" class="repair-card">
                    <div class="repair-info">
                      <h4>针对 {{ conflictGroups[activeGroupIndex]?.subject }} 的方案</h4>
                      <el-radio-group v-model="conflictGroups[activeGroupIndex].selectedResolution">
                        <el-radio label="overwrite" border class="repair-radio">
                          <strong>覆盖更新</strong>
                          <p>采用新输入值 ({{ conflictGroups[activeGroupIndex]?.newValue }})</p>
                        </el-radio>
                        <el-radio label="keep" border class="repair-radio">
                          <strong>保持现状</strong>
                          <p>忽略本次输入，保留原库值 ({{ conflictGroups[activeGroupIndex]?.oldValue }})</p>
                        </el-radio>
                        <el-radio label="manual" border class="repair-radio">
                          <strong>人工修正</strong>
                          <el-input
                            size="small"
                            v-model="conflictGroups[activeGroupIndex].customValue"
                            placeholder="输入修正后的值"
                            :disabled="conflictGroups[activeGroupIndex].selectedResolution !== 'manual'"
                            @click.stop
                            style="margin-top: 8px;"
                          />
                        </el-radio>
                      </el-radio-group>
                    </div>
                    <div class="repair-actions" style="margin-top: 20px;">
                      <el-button type="success" :loading="loadingRepair" @click="handleSingleRepair">
                        确认并应用修复
                      </el-button>
                    </div>
                  </el-card>
                  <el-empty v-else description="请点击左侧冲突项查看方案" />
                </el-col>
              </el-row>

              <el-result
                v-show="conflictStep === 5"
                icon="success"
                title="图谱更新完成"
                sub-title="所有合法数据及修复后的冲突数据，已安全同步至 Neo4j 数据库。"
                style="background: #fff; border-radius: 8px; margin-top: 20px; box-shadow: 0 2px 12px 0 rgba(0,0,0,0.05);"
              >
                <template #extra>
                  <el-button type="primary" @click="resetConflictFlow">
                    继续添加新数据
                  </el-button>
                </template>
              </el-result>
            </div>
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

.triples-textarea, .json-textarea {
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

.card-header-flex {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.turtle-editor {
  font-family: 'Fira Code', monospace;
  :deep(.el-textarea__inner) {
    background-color: #f8f9fa;
    color: #333;
  }
}

.conflict-item-card {
  margin-bottom: 12px;
  cursor: pointer;
  border-left: 4px solid #f56c6c;
  transition: all 0.3s;
  &.is-active {
    border-left-width: 8px;
    background-color: #fef0f0;
    transform: translateX(5px);
  }
}

.comparison-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 10px;
  .val-box {
    padding: 8px;
    border-radius: 4px;
    font-size: 12px;
    .label { display: block; color: #909399; margin-bottom: 4px; }
    &.old { background: #f4f4f5; }
    &.new { background: #fef0f0; color: #f56c6c; font-weight: bold; }
  }
}

.repair-radio {
  display: block;
  width: 100%;
  margin-bottom: 10px;
  height: auto !important;
  padding: 12px !important;
  strong { display: block; margin-bottom: 4px; }
  p { margin: 0; font-size: 12px; color: #666; }
}

.scroll-container {
  max-height: 600px;
  overflow-y: auto;
  padding-right: 5px;
}

.action-buttons {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}

.stat-summary {
  display: flex;
  align-items: center;
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
.algorithm-annotation-card {
  margin-bottom: 24px;
  border: 1px solid #ebeef5;
  border-radius: 4px; /* 如果想要截图那种稍微硬朗的直角，可以设为 2px 或 4px */
  background-color: #ffffff;
}

/* 头部样式：粗体、大号字、带底边框 */
.card-header-wrapper {
  display: flex;
  align-items: center;
  padding-bottom: 12px;
  border-bottom: 1px solid #ebeef5;
  margin-bottom: 16px;
}

.header-icon {
  font-size: 20px;
  margin-right: 10px;
}

.header-title {
  font-size: 16px;
  font-weight: bold;
  color: #303133;
}

/* 简述文本 */
.main-description {
  font-size: 14px;
  color: #606266;
  margin-bottom: 24px;
  line-height: 1.6;
}

/* 特征块排版 */
.features-row {
  margin-bottom: 24px;
}

.feature-block {
  display: flex;
  align-items: flex-start;
}

.feature-icon {
  font-size: 24px;
  margin-right: 12px;
  color: #909399; 

}

.feature-title {
  font-size: 15px;
  font-weight: bold;
  color: #303133;
  margin-bottom: 6px;
}

.feature-desc {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
</style>