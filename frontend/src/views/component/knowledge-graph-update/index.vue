<script setup>
import { ref, reactive, onMounted, watch, nextTick, onUnmounted } from 'vue'//part new
import { Link, Warning, MagicStick, Loading, Plus, Delete, DataLine, WarningFilled } from '@element-plus/icons-vue'//part new
import { kgUpdateHealthCheck, kgUpdateAdd, kgUpdateDelete, kgUpdateStatistics } from '@/api'
import { ElMessage } from 'element-plus'
import axios from 'axios' // 🎯 这一行是你新加的new
import { Network } from 'vis-network'

const loadingAdd = ref(false)
const loadingDelete = ref(false)
const loadingStats = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

// new
const turtleInput = ref('')
const conflictGroups = ref([]) // 存储冲突详情
const activeGroupIndex = ref(null) // 当前选中的冲突项
const loadingDetect = ref(false)
const loadingRepair = ref(false)
const visContainer = ref(null)
let networkInstance = null

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

// 新增：侧边栏活动项
const activeMenu = ref('confidence')

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

// 处理文件上传new
const handleFileUpload = (file) => {
  const reader = new FileReader()
  reader.onload = (e) => {
    turtleInput.value = e.target.result
    ElMessage.success('文件加载成功')
  }
  reader.readAsText(file.raw)
}
const currentStep = ref(0)
// 真实的冲突检测接口
const handleConflictDetect = async () => {
  if (!turtleInput.value.trim()) return ElMessage.warning('请先输入 Turtle 数据')
  
  loadingDetect.value = true
  conflictGroups.value = [] 
  currentStep.value = 1 
  
  try {
    const { data } = await axios.post('/api/conflict/detect', {
      insert_ttl: turtleInput.value,
      delete_ttl: "" 
    })
    
    if (data.success) {
      conflictGroups.value = data.conflicts || []
      
      if (conflictGroups.value.length > 0) {
        // 🎯 优化点 1：精准播报。哪怕后端 message 没传，前端也能报数
        const msg = data.message || `冲突检测完成：发现 ${conflictGroups.value.length} 项违规数据。`
        
        activeGroupIndex.value = 0 
        currentStep.value = 3 // 🎯 注意：根据你的 UI 图，第 3 步才是“展示与修复”
        
        ElMessage({
          message: msg,
          type: 'warning',
          duration: 5000, // 多留一会儿，让用户看清楚
          showClose: true
        })
      } else {
        // 🎯 优化点 2：全线通过
        activeGroupIndex.value = null
        currentStep.value = 5 
        ElMessage.success('🚀 冲突检测完成：未发现任何冲突，数据已自动同步！')
      }
    }
  } catch (err) {
    console.error('检测接口报错:', err)
    currentStep.value = 0 
    ElMessage.error(err?.response?.data?.detail || '检测失败，请检查后端服务')
  } finally {
    loadingDetect.value = false
  }
}

// 真实的执行修复接口
const handleSingleRepair = async () => {
  if (activeGroupIndex.value === null) return
  
  const current = conflictGroups.value[activeGroupIndex.value]
  loadingRepair.value = true
  currentStep.value = 3 
  
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
        currentStep.value = 2 
      } else {
        activeGroupIndex.value = null
        // 🎯 修改点：全部冲突修完后，跳到 5，让全流程变绿
        currentStep.value = 5 
      }
    }
  } catch (err) {
    console.error('修复接口报错:', err)
    currentStep.value = 2 
    ElMessage.error(err?.response?.data?.detail || '修复执行失败')
  } finally {
    loadingRepair.value = false
  }
}


// 监听步骤变化
watch(currentStep, async (newVal) => {
  if (newVal === 2 || newVal === 3) {
    await nextTick()
    renderConflictGraph()
  }
})

const renderConflictGraph = () => {
  if (!visContainer.value) return;

  // 1. 清理旧图
  if (networkInstance) {
    networkInstance.destroy();
    networkInstance = null;
  }

  const nodes = [];
  const edges = [];

  // 🎯 核心修复：把后端传回来的 conflictGroups 变成 Vis-network 能认出的圆圈
  conflictGroups.value.forEach((conflict) => {
    // A. 创建“冲突主体”节点 (比如 G006)
    nodes.push({
      id: conflict.subject,
      label: `${conflict.subject}\n(违规主体)`,
      color: { background: '#fef0f0', border: '#f56c6c' }, // 红色警告
      shape: 'dot',
      size: 35,
      font: { multi: 'md', color: '#f56c6c', weight: 'bold' }
    });

    // B. 创建“现有值”节点 (如果数据库里有老数据)
    if (conflict.oldValue !== '无记录') {
      const oldNodeId = `old_${conflict.subject}`;
      nodes.push({
        id: oldNodeId,
        label: `库内原值:\n${conflict.oldValue}`,
        color: { background: '#f0f9eb', border: '#67c23a' }, // 绿色存量
        shape: 'box',
        size: 20
      });
      // 连一根实线表示“当前连接”
      edges.push({ from: conflict.subject, to: oldNodeId, label: '当前属性', color: '#67c23a' });
    }

    // C. 创建“新输入值”节点
    const newNodeId = `new_${conflict.subject}`;
    nodes.push({
      id: newNodeId,
      label: `拟输入值:\n${conflict.newValue}`,
      color: { background: '#fdf6ec', border: '#e6a23c' }, // 橙色拟变
      shape: 'box',
      size: 20
    });
    // 连一根虚线表示“冲突的拟更新”
    edges.push({ from: conflict.subject, to: newNodeId, label: '冲突更新', dashes: true, color: '#e6a23c' });
  });

  // 2. 初始化图谱
  const options = {
    physics: {
      enabled: true,
      barnesHut: { gravitationalConstant: -3000, springLength: 150 } // 增加排斥力，防止重叠
    },
    interaction: { hover: true, tooltipDelay: 200 }
  };

  networkInstance = new Network(visContainer.value, { nodes, edges }, options);

  // 3. 重新绑定联动逻辑 (确保点击圆圈能联动列表)
  networkInstance.on("click", (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      // 如果点的是主体或者是衍生节点，都尝试匹配列表
      const subjectId = nodeId.replace('old_', '').replace('new_', '');
      const index = conflictGroups.value.findIndex(c => c.subject === subjectId);
      if (index !== -1) {
        activeGroupIndex.value = index;
      }
    }
  });
};
onUnmounted(() => {
  if (networkInstance) networkInstance.destroy()
})
// --- 前端脚本部分 ---
// ==========================================
// 🎯 1. 核心修复逻辑 (对象结构化版)
// ==========================================
const handleApplyRepair = async () => {
  const index = activeGroupIndex.value;
  if (index === null || !conflictGroups.value[index]) return;

  const current = conflictGroups.value[index];
  const resolution = current.selectedResolution;

  let finalValue = '';
  if (resolution === 'manual') {
    if (!current.customValue) return ElMessage.warning('请输入人工修正值');
    finalValue = current.customValue;
  } else if (resolution === 'overwrite') {
    finalValue = current.newValue;
  } else if (resolution === 'keep') {
    return handleFinishOrNext();
  }

  // 构造结构化数据
  const repairAction = {
    subject: current.subject.replace(/^ex:/, ''),
    predicate: current.predicate.replace(/^ex:/, ''),
    value: finalValue,
    operation: 'add'
  };

  try {
    const { data } = await axios.post('/api/conflict/repair', {
      actions: [repairAction]
    });

    if (data.success) {
      ElMessage.success(`修正成功: ${finalValue}`);
      handleFinishOrNext(); // 🎯 执行跳转逻辑
    }
  } catch (err) {
    console.error('修复失败:', err);
    ElMessage.error(err.response?.data?.detail || '数据库写入异常');
  }
};

// ==========================================
// 🎯 2. 辅助跳转逻辑 (确保逻辑不嵌套)
// ==========================================
const handleFinishOrNext = () => {
  if (activeGroupIndex.value === conflictGroups.value.length - 1) {
    // 最后一张卡片
    currentStep.value = 5; 
    ElMessage.success('🎉 所有冲突已处理完毕！');
  } else {
    // 切换到下一张
    proceedToNext();
  }
};
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
            <span>冲突检测更新</span>
          </el-menu-item>
        </el-menu>
        <div class="main-content">
          <!-- 置信度计算与更新内容 -->
          <div v-if="activeMenu === 'confidence'">
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
          <!-- 冲突检测与修复内容（空页面） -->
          <div v-else-if="activeMenu === 'conflict'" class="conflict-split-view">
  
            <el-card shadow="never" style="margin-bottom: 20px; border: none; background: transparent;">
              <el-steps :active="currentStep" finish-status="success" align-center>
                <el-step title="数据输入" />
                <el-step title="冲突检测" />
                <el-step title="展示与修复" />
                <el-step title="正在修复" />
                <el-step title="更新完成" />
              </el-steps>
            </el-card>

            <div class="conflict-wrapper">

              <el-card class="form-card" shadow="hover" v-show="currentStep < 2">
                <template #header>
                  <div class="card-header-flex">
                    <span>RDF Turtle 数据输入</span>
                    <el-upload action="#" :auto-upload="false" :show-file-list="false" @change="handleFileUpload">
                      <el-button type="primary" link :icon="Link">上传 .ttl 文件</el-button>
                    </el-upload>
                  </div>
                </template>
                <el-input
                  v-model="turtleInput"
                  type="textarea"
                  :rows="8"
                  placeholder="@prefix ex: <http://example.org/> ... "
                  class="turtle-editor"
                  @input="currentStep = 0"
                />
                <div style="margin-top: 12px; display: flex; gap: 10px;">
                  <el-button type="primary" :loading="loadingDetect" @click="handleConflictDetect">
                    开始冲突检测
                  </el-button>
                  <el-button @click="turtleInput = ''; currentStep = 0">清空</el-button>
                </div>
              </el-card>

              <el-card class="graph-card" shadow="hover" v-show="currentStep === 2 || currentStep === 3" style="margin-bottom: 20px;">
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

              <el-row :gutter="20" v-show="(currentStep === 2 || currentStep === 3) && conflictGroups.length > 0" style="margin-top: 20px;">
                <el-col :span="12">
                  <h3 class="sub-title"><el-icon><Warning /></el-icon> 发现冲突项</h3>
                  <div class="scroll-container">
                    <el-card 
                      v-for="(group, index) in conflictGroups" 
                      :key="index" 
                      class="conflict-item-card"
                      :class="{ 'is-active': activeGroupIndex === index }"
                      @click="activeGroupIndex = index"
                    >
                      <div class="conflict-header">
                        <el-tag type="danger" size="small">{{ group.conflictType }}</el-tag>
                        <span class="subject-text">{{ group.subject }}</span>
                      </div>
                      <p class="predicate-text">属性：<code>{{ group.predicate }}</code></p>
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
                      <h4>针对 {{ conflictGroups[activeGroupIndex].subject }} 的方案</h4>
                      <el-radio-group v-model="conflictGroups[activeGroupIndex].selectedResolution">
                        <el-radio label="overwrite" border class="repair-radio">
                          <strong>覆盖更新</strong>
                          <p>采用新输入值 ({{ conflictGroups[activeGroupIndex].newValue }})</p>
                        </el-radio>
                        <el-radio label="keep" border class="repair-radio">
                          <strong>保持现状</strong>
                          <p>忽略本次输入，保留原库值 ({{ conflictGroups[activeGroupIndex].oldValue }})</p>
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
                v-show="currentStep === 5"
                icon="success"
                title="图谱更新完成"
                sub-title="所有合法数据及修复后的冲突数据，已安全同步至 Neo4j 数据库。"
                style="background: #fff; border-radius: 8px; margin-top: 20px; box-shadow: 0 2px 12px 0 rgba(0,0,0,0.05);"
              >
                <template #extra>
                  <el-button type="primary" @click="turtleInput = ''; currentStep = 0; conflictGroups = []">
                    继续添加新数据
                  </el-button>
                </template>
              </el-result>

          </div> </div>
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
</style>
