<script setup>
import { ref, onMounted, nextTick, watch } from 'vue'
import { ChatDotRound, PictureFilled, QuestionFilled, SetUp, Document, ArrowRight, ArrowDown } from '@element-plus/icons-vue'
import {
  answerGenerationHealthCheck,
  answerGenerationAsk,
  answerGenerationAskVisualize,
  answerGenerationQueryGraph
} from '@/api'
import { ElMessage } from 'element-plus'
import cytoscape from 'cytoscape'

// 子组件：问题定义
const ProblemDefinition = {
  props: {
    data: Object
  },
  setup(props) {
    const expandedSubProblems = ref([])
    const hoveredSubProblem = ref(null)
    
    const toggleSubProblem = (id) => {
      const index = expandedSubProblems.value.indexOf(id)
      if (index > -1) {
        expandedSubProblems.value.splice(index, 1)
      } else {
        expandedSubProblems.value.push(id)
      }
    }
    
    return { expandedSubProblems, hoveredSubProblem, toggleSubProblem }
  },
  template: `
    <div class="problem-definition">
      <h4 class="component-title">
        <el-icon><QuestionFilled /></el-icon>
        问题定义
      </h4>
      
      <!-- 根问题 -->
      <div class="root-problem" v-if="data?.root_problem">
        <div class="problem-header">
          <span class="problem-tag root">根问题</span>
          <span class="problem-name">{{ data.root_problem.name }}</span>
        </div>
        <div class="problem-info">
          <p><strong>类型：</strong>{{ data.root_problem.type }}</p>
          <p><strong>定义：</strong>{{ data.root_problem.definition }}</p>
        </div>
      </div>
      
      <!-- 子问题列表 -->
      <div class="sub-problems" v-if="data?.sub_problems?.length > 0">
        <div class="section-title">
          <span>子问题分解 ({{ data.sub_problems.length }}个)</span>
        </div>
        <div class="sub-problem-list">
          <div 
            v-for="sp in data.sub_problems" 
            :key="sp.id"
            class="sub-problem-item"
            :class="{ expanded: expandedSubProblems.includes(sp.id) }"
          >
            <div 
              class="sub-problem-header"
              @click="toggleSubProblem(sp.id)"
              @mouseenter="hoveredSubProblem = sp"
              @mouseleave="hoveredSubProblem = null"
            >
              <el-icon class="expand-icon">
                <ArrowRight v-if="!expandedSubProblems.includes(sp.id)" />
                <ArrowDown v-else />
              </el-icon>
              <span class="problem-tag sub">子问题</span>
              <span class="problem-name">{{ sp.name }}</span>
            </div>
            
            <!-- 展开的子问题详情 -->
            <div v-if="expandedSubProblems.includes(sp.id)" class="sub-problem-detail">
              <p><strong>类型：</strong>{{ sp.type }}</p>
              <p><strong>定义：</strong>{{ sp.definition }}</p>
            </div>
            
            <!-- 悬停显示的子问题定义浮窗 -->
            <div v-if="hoveredSubProblem?.id === sp.id && !expandedSubProblems.includes(sp.id)" 
                 class="sub-problem-tooltip">
              <p><strong>{{ sp.name }}</strong></p>
              <p>{{ sp.definition }}</p>
            </div>
          </div>
        </div>
      </div>
      
      <div v-else class="empty-state">
        <el-empty description="暂无子问题分解信息" />
      </div>
    </div>
  `
}

// 子组件：方案规划
const SolutionPlanning = {
  props: {
    data: Object
  },
  template: `
    <div class="solution-planning">
      <h4 class="component-title">
        <el-icon><SetUp /></el-icon>
        方案规划
      </h4>
      
      <div v-if="data?.solutions?.length > 0" class="solution-list">
        <div v-for="(sol, index) in data.solutions" :key="sol.id" class="solution-card">
          <div class="solution-header">
            <span class="solution-number">方案 {{ index + 1 }}</span>
            <span class="solution-name">{{ sol.name }}</span>
          </div>
          
          <div class="solution-info">
            <p><strong>类型：</strong>{{ sol.type }}</p>
            
            <div v-if="sol.inputs?.length > 0" class="info-section">
              <p class="section-label">输入：</p>
              <ul class="info-list">
                <li v-for="(input, idx) in sol.inputs" :key="idx">{{ input }}</li>
              </ul>
            </div>
            
            <div v-if="sol.outputs?.length > 0" class="info-section">
              <p class="section-label">输出：</p>
              <ul class="info-list">
                <li v-for="(output, idx) in sol.outputs" :key="idx">{{ output }}</li>
              </ul>
            </div>
            
            <div v-if="sol.steps?.length > 0" class="info-section">
              <p class="section-label">执行步骤：</p>
              <ol class="step-list">
                <li v-for="(step, idx) in sol.steps" :key="idx">{{ step }}</li>
              </ol>
            </div>
          </div>
        </div>
      </div>
      
      <div v-else class="empty-state">
        <el-empty description="暂无方案规划信息" />
      </div>
    </div>
  `
}

// 子组件：答案展示
const AnswerDisplay = {
  props: {
    answer: String,
    loading: Boolean
  },
  template: `
    <div class="answer-display">
      <h4 class="component-title">
        <el-icon><Document /></el-icon>
        答案
      </h4>
      
      <div v-if="loading" class="loading-state">
        <el-skeleton :rows="3" animated />
      </div>
      
      <div v-else-if="answer" class="answer-content">
        <div class="answer-text">{{ answer }}</div>
      </div>
      
      <div v-else class="empty-state">
        <el-empty description="请输入问题并点击获取答案" />
      </div>
    </div>
  `
}

const loading = ref(false)
const loadingViz = ref(false)
const loadingGraph = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')
const question = ref('')
const result = ref(null)
const resultWithViz = ref(null)
const activeTab = ref('answer')

watch(activeTab, (newTab) => {
  if (newTab === 'visualization' && result.value) {
    queryAndRenderGraph()
  }
})
const cyInstance = ref(null)
const graphData = ref(null)
const graphContainerRef = ref(null)

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await answerGenerationHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '答案生成模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端并配置 algorithm/ans（Neo4j、LLM 等）'
  }
}

const submit = async () => {
  const q = question.value?.trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  loading.value = true
  result.value = null
  resultWithViz.value = null
  try {
    const response = await answerGenerationAsk({ question: q, detailed: true })
    
    if (!response || !response.data) {
      ElMessage.error('后端返回数据为空')
      return
    }
    
    result.value = response.data
    if (response.data) {
      ElMessage.success('回答完成')
      activeTab.value = 'answer'
    } else {
      ElMessage.warning('未得到有效答案')
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '请求失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

const submitWithVisualization = async () => {
  const q = question.value?.trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  loadingViz.value = true
  result.value = null
  resultWithViz.value = null
  graphData.value = null
  try {
    const { data } = await answerGenerationAskVisualize({ question: q })
    resultWithViz.value = data
    ElMessage.success(data?.success ? '回答与可视化完成' : '未得到有效答案')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '请求失败')
    resultWithViz.value = null
  } finally {
    loadingViz.value = false
  }
}

const extractNodeLabelsFromResult = (result) => {
  const labels = new Set()
  if (result?.raw_details?.图谱路径?.answer_path) {
    for (const path of result.raw_details.图谱路径.answer_path) {
      if (path.nodes) {
        for (const node of path.nodes) {
          if (node.label) {
            labels.add(node.label)
          }
        }
      }
    }
  }
  return Array.from(labels)
}

const buildGraphFromReasoningPath = (result) => {
  console.log('从推理路径构建图谱数据')
  const nodes = []
  const edges = []
  const nodeMap = {}
  
  if (result?.raw_details?.图谱路径?.answer_path) {
    let nodeIdCounter = 1
    for (const path of result.raw_details.图谱路径.answer_path) {
      if (path.nodes) {
        for (const node of path.nodes) {
          if (!nodeMap[node.label]) {
            const nodeId = nodeIdCounter++
            nodeMap[node.label] = nodeId
            nodes.push({
              id: nodeId,
              name: node.label,
              labels: [node.label]
            })
          }
        }
      }
      
      if (path.relationships) {
        let edgeIdCounter = 1
        for (const rel of path.relationships) {
          const fromId = nodeMap[rel.from]
          const toId = nodeMap[rel.to]
          if (fromId && toId) {
            edges.push({
              id: edgeIdCounter++,
              from: fromId,
              to: toId,
              type: rel.type
            })
          }
        }
      }
    }
  }
  
  console.log('从推理路径构建的节点:', nodes)
  console.log('从推理路径构建的关系:', edges)
  return { nodes, relationships: edges }
}

const queryAndRenderGraph = async () => {
  console.log('queryAndRenderGraph 被调用')
  if (!result.value) {
    console.log('没有 result 数据')
    return
  }
  
  const nodeLabels = extractNodeLabelsFromResult(result.value)
  console.log('提取到的节点标签:', nodeLabels)
  if (nodeLabels.length === 0) {
    console.log('没有节点标签，尝试从推理路径构建')
    const fallbackGraph = buildGraphFromReasoningPath(result.value)
    if (fallbackGraph.nodes.length > 0) {
      renderGraph(fallbackGraph)
      graphData.value = fallbackGraph
      await nextTick()
      renderGraph(fallbackGraph)
    }
    return
  }
  
  loadingGraph.value = true
  try {
    console.log('正在调用查询图谱 API...')
    const response = await answerGenerationQueryGraph({ node_labels: nodeLabels, limit: 30 })
    console.log('API 返回完整响应:', response)
    
    const data = response.data
    console.log('响应数据:', data)
    
    if (data?.success && data?.data && data.data.nodes?.length > 0) {
      console.log('准备调用 renderGraph')
      renderGraph(data.data)
      console.log('设置图谱数据:', data.data)
      graphData.value = data.data
      await nextTick()
      console.log('准备调用 renderGraph')
      renderGraph(data.data)
    } else {
        renderGraph(fallbackGraph)
      console.log('API 没有返回足够的数据，尝试从推理路径构建')
      const fallbackGraph = buildGraphFromReasoningPath(result.value)
      if (fallbackGraph.nodes.length > 0) {
        graphData.value = fallbackGraph
        await nextTick()
        renderGraph(fallbackGraph)
      }
    }
  } catch (err) {
      renderGraph(fallbackGraph)
    console.error('查询图谱失败:', err)
    console.log('查询失败，尝试从推理路径构建')
    const fallbackGraph = buildGraphFromReasoningPath(result.value)
    if (fallbackGraph.nodes.length > 0) {
      graphData.value = fallbackGraph
      await nextTick()
      renderGraph(fallbackGraph)
    }
  } finally {
    loadingGraph.value = false
  }
}

const renderGraph = (graphData) => {
  console.log('renderGraph 被调用，数据:', graphData)

  if (cyInstance.value) {
    cyInstance.value.destroy()
    cyInstance.value = null
  }

  if (!graphContainerRef.value) return

  const elements = {
    nodes: (graphData?.nodes || []).map((node) => ({
      data: {
        id: String(node.id ?? node.name ?? ''),
        label: node.name || String(node.id),
        ...node
      }
    })),
    edges: (graphData?.relationships || []).map((rel, idx) => ({
      data: {
        id: String(rel.id ?? idx),
        source: String(rel.from ?? rel.source ?? ''),
        target: String(rel.to ?? rel.target ?? ''),
        label: rel.type || rel.label || '',
        ...rel
      }
    }))
  }

  cyInstance.value = cytoscape({
    container: graphContainerRef.value,
    elements,
    style: [
      {
        selector: 'node',
        style: {
          'background-color': '#0ea5e9',
          'label': 'data(label)',
          'color': '#fff',
          'text-valign': 'center',
          'text-halign': 'center',
          'font-size': '12px',
          'width': '40px',
          'height': '40px',
          'shape': 'ellipse'
        }
      },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': '#94a3b8',
          'target-arrow-color': '#94a3b8',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'label': 'data(label)',
          'font-size': '10px',
          'color': '#64748b',
          'text-rotation': 'autorotate'
        }
      }
    ],
    layout: {
      name: 'cose',
      animate: true,
      animationDuration: 500,
      fit: true,
      padding: 30
    },
    ready: function () {
      console.log('cytoscape 准备就绪')
    }
  })

  console.log('cytoscape 实例已创建:', cyInstance.value)
}

watch(resultWithViz, (newVal) => {
  if (newVal) {
    queryAndRenderGraph()
  }
})

onMounted(checkHealth)
</script>

<template>
  <div class="answer-view">
    <div class="answer-inner">
      <h1 class="page-title">
        <span class="title-highlight">答案生成</span>
      </h1>
      <p class="page-desc">
        用户输入自然语言问题后，系统会自动进行问题定义与分析，然后从知识图谱中检索相关信息，通过多步推理生成答案。同时提供完整的问题求解过程、方案规划和知识图谱的可视化展示。
      </p>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">服务状态：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <!-- 问题输入区域 -->
      <el-card class="input-card" shadow="hover">
        <template #header>
          <span>问题输入</span>
        </template>
        <el-form class="answer-form">
          <el-form-item label="问题" required>
            <el-input
              v-model="question"
              type="textarea"
              :rows="3"
              placeholder="输入基于知识图谱的自然语言问题，例如：列车制动系统常见故障有哪些？"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              :icon="ChatDotRound"
              @click="submit"
            >
              获取答案
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- 结果展示区域 - 使用标签页 -->
      <el-card v-if="result !== null" class="result-card" shadow="hover">
        <el-tabs v-model="activeTab" type="border-card">
          <el-tab-pane label="答案" name="answer">
            <div v-if="loading" class="loading-state">
              <el-skeleton :rows="3" animated />
            </div>
            <div v-else-if="result.answer">
              <div class="answer-text">{{ result.answer }}</div>
            </div>
            <div v-else class="empty-state">
              <el-empty description="请输入问题并点击获取答案" />
            </div>
          </el-tab-pane>
          
          <el-tab-pane label="问题定义" name="problem">
            <div v-if="result.raw_details">
              <div v-if="result.raw_details.输入问题" style="margin-bottom: 20px;">
                <p><strong>输入问题：</strong>{{ result.raw_details.输入问题 }}</p>
              </div>
              
              <div v-if="result.raw_details.问题模型">
                <h4 style="margin-bottom: 12px;">问题模型</h4>
                <el-table :data="[{ ...result.raw_details.问题模型 }]" border style="width: 100%; margin-bottom: 20px;">
                  <el-table-column prop="问题ID" label="问题ID" width="120" />
                  <el-table-column prop="问题描述" label="问题描述" min-width="150" />
                  <el-table-column prop="问题类型" label="问题类型" width="120" />
                  <el-table-column label="约束" min-width="150">
                    <template #default="scope">
                      {{ scope.row.约束 && scope.row.约束.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="目标对象" label="目标对象" width="120" />
                  <el-table-column label="干系人" min-width="150">
                    <template #default="scope">
                      {{ scope.row.干系人 && scope.row.干系人.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column label="目标" min-width="150">
                    <template #default="scope">
                      {{ scope.row.目标 && scope.row.目标.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="当前状态" label="当前状态" width="100" />
                  <el-table-column prop="创建时间" label="创建时间" width="180" />
                  <el-table-column label="相关实体" min-width="150">
                    <template #default="scope">
                      {{ scope.row.相关实体 && scope.row.相关实体.join('、') }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
              
              <div v-if="result.raw_details.子问题 && result.raw_details.子问题.length > 0">
                <h4 style="margin-bottom: 12px;">子问题</h4>
                <el-table :data="result.raw_details.子问题" border style="width: 100%;">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="问题ID" label="问题ID" width="120" />
                  <el-table-column prop="问题描述" label="问题描述" min-width="150" />
                  <el-table-column prop="问题类型" label="问题类型" width="120" />
                  <el-table-column label="约束" min-width="150">
                    <template #default="scope">
                      {{ scope.row.约束 && scope.row.约束.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="目标对象" label="目标对象" width="120" />
                  <el-table-column label="干系人" min-width="150">
                    <template #default="scope">
                      {{ scope.row.干系人 && scope.row.干系人.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column label="目标" min-width="150">
                    <template #default="scope">
                      {{ scope.row.目标 && scope.row.目标.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="当前状态" label="当前状态" width="100" />
                  <el-table-column prop="创建时间" label="创建时间" width="180" />
                  <el-table-column label="相关实体" min-width="150">
                    <template #default="scope">
                      {{ scope.row.相关实体 && scope.row.相关实体.join('、') }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </el-tab-pane>
          
          <el-tab-pane label="方案规划" name="solution">
            <div v-if="result.raw_details">
              <div v-if="result.raw_details.方案模型 && result.raw_details.方案模型.length > 0">
                <h4 style="margin-bottom: 12px;">方案模型</h4>
                <el-table :data="result.raw_details.方案模型" border style="width: 100%; margin-bottom: 20px;">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="方案ID" label="方案ID" width="120" />
                  <el-table-column prop="方案类别" label="方案类别" width="150" />
                  <el-table-column prop="方案目标" label="方案目标" min-width="200" />
                  <el-table-column label="输入" min-width="150">
                    <template #default="scope">
                      {{ scope.row.输入 && scope.row.输入.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column label="输出" min-width="200">
                    <template #default="scope">
                      {{ scope.row.输出 && scope.row.输出.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column label="约束" min-width="150">
                    <template #default="scope">
                      {{ scope.row.约束 && scope.row.约束.join('、') }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="控制逻辑" label="控制逻辑" min-width="180" />
                  <el-table-column label="置信度" width="100">
                    <template #default="scope">
                      {{ scope.row.置信度 !== undefined ? scope.row.置信度.toFixed(2) : '' }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
              
              <div v-if="result.raw_details['问题-方案匹配'] && result.raw_details['问题-方案匹配'].length > 0">
                <h4 style="margin-bottom: 12px;">问题-方案匹配</h4>
                <el-table :data="result.raw_details['问题-方案匹配']" border style="width: 100%;">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="问题ID" label="问题ID" width="120" />
                  <el-table-column prop="方案ID" label="方案ID" width="120" />
                  <el-table-column prop="匹配度" label="匹配度" width="100" />
                  <el-table-column prop="匹配理由" label="匹配理由" min-width="200" />
                </el-table>
              </div>
            </div>
          </el-tab-pane>
          
          <el-tab-pane label="推理细节" name="reasoning">
            <div v-if="result.raw_details && result.raw_details.推理日志" class="reasoning-logs">
              <div v-for="(log, index) in result.raw_details.推理日志" :key="index" class="log-item">
                <div class="log-header">
                  <span class="log-step">{{ log.step }}</span>
                  <span class="log-message">{{ log.message }}</span>
                </div>
                <div v-if="log.data" class="log-data">
                  <pre v-if="typeof log.data === 'string'">{{ log.data }}</pre>
                  <pre v-else>{{ JSON.stringify(log.data, null, 2).replace(/\\n/g, '\n') }}</pre>
                </div>
              </div>
            </div>
            <div v-else class="empty-state">
              <el-empty description="暂无推理细节信息" />
            </div>
          </el-tab-pane>
          
          <el-tab-pane label="可视化" name="visualization">
            <div v-if="result.raw_details && result.raw_details.图谱路径">
              <div v-if="result.raw_details.图谱路径.reasoning_chains && result.raw_details.图谱路径.reasoning_chains.length > 0">
                <h4 style="margin-bottom: 12px;">推理路径</h4>
                <div class="reasoning-chains">
                  <div v-for="(chain, chainIndex) in result.raw_details.图谱路径.reasoning_chains" :key="chainIndex" class="chain-group">
                    <div v-for="(path, pathIndex) in chain" :key="pathIndex" class="chain-path">
                      <span v-for="(item, itemIndex) in path" :key="itemIndex" class="chain-item">
                        <span class="entity">{{ item }}</span>
                        <span v-if="itemIndex < path.length - 1 && itemIndex % 3 === 0" class="relation"> → </span>
                        <span v-else-if="itemIndex < path.length - 1 && itemIndex % 3 === 1" class="relation-arrow"> → </span>
                      </span>
                    </div>
                  </div>
                </div>
              </div>
              
              <div v-if="loadingGraph" class="graph-loading">
                <el-skeleton :rows="5" animated />
              </div>
              <div v-else-if="graphData" class="graph-container-wrapper">
                <h4 style="margin-bottom: 12px;">知识图谱可视化</h4>
                <div ref="graphContainerRef" class="graph-container"></div>
              </div>
              <div v-else>
                <p class="viz-placeholder">未生成可视化内容</p>
              </div>
            </div>
            <div v-else class="empty-state">
              <el-empty description="暂无可视化信息" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.answer-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.answer-inner {
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

.input-card, .result-card {
  border-radius: var(--card-radius-lg);
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.answer-form {
  max-width: 640px;
}

// 组件通用样式
.component-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-800);
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--gray-200);
}

// 问题定义组件样式
.problem-definition {
  .root-problem {
    background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
    border-left: 4px solid #0ea5e9;
  }
  
  .problem-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }
  
  .problem-tag {
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    
    &.root {
      background: #0ea5e9;
      color: white;
    }
    
    &.sub {
      background: #e0e7ff;
      color: #4338ca;
    }
  }
  
  .problem-name {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-800);
  }
  
  .problem-info {
    p {
      margin: 8px 0;
      color: var(--gray-700);
      line-height: 1.6;
    }
  }
  
  .section-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--gray-700);
    margin-bottom: 12px;
  }
  
  .sub-problems {
    margin-top: 20px;
  }
  
  .sub-problem-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  
  .sub-problem-item {
    position: relative;
    background: white;
    border-radius: 8px;
    border: 1px solid var(--gray-200);
    transition: all 0.3s ease;
    
    &:hover {
      border-color: #818cf8;
      box-shadow: 0 2px 8px rgba(129, 140, 248, 0.15);
    }
    
    &.expanded {
      border-color: #818cf8;
    }
  }
  
  .sub-problem-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 16px;
    cursor: pointer;
    user-select: none;
    
    &:hover {
      background: #f8fafc;
    }
  }
  
  .expand-icon {
    color: var(--gray-500);
    transition: transform 0.2s;
  }
  
  .sub-problem-detail {
    padding: 0 16px 16px 48px;
    border-top: 1px solid var(--gray-100);
    
    p {
      margin: 8px 0;
      color: var(--gray-700);
      font-size: 14px;
      line-height: 1.6;
    }
  }
  
  .sub-problem-tooltip {
    position: absolute;
    left: 48px;
    right: 16px;
    bottom: -60px;
    background: white;
    border: 1px solid #818cf8;
    border-radius: 8px;
    padding: 12px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    z-index: 100;
    
    p {
      margin: 4px 0;
      font-size: 13px;
      color: var(--gray-700);
      
      &:first-child {
        font-weight: 600;
        color: var(--gray-800);
      }
    }
  }
}

// 方案规划组件样式
.solution-planning {
  .solution-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  
  .solution-card {
    background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
    border-radius: 12px;
    padding: 20px;
    border-left: 4px solid #22c55e;
  }
  
  .solution-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }
  
  .solution-number {
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    background: #22c55e;
    color: white;
  }
  
  .solution-name {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-800);
  }
  
  .solution-info {
    p {
      margin: 8px 0;
      color: var(--gray-700);
    }
  }
  
  .info-section {
    margin-top: 12px;
  }
  
  .section-label {
    font-weight: 600;
    color: var(--gray-800);
    margin-bottom: 8px;
  }
  
  .info-list, .step-list {
    margin: 0;
    padding-left: 20px;
    
    li {
      margin: 4px 0;
      color: var(--gray-700);
      line-height: 1.5;
    }
  }
  
  .step-list {
    li {
      margin: 6px 0;
    }
  }
}

// 答案展示组件样式
.answer-display {
  .answer-content {
    background: linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);
    border-radius: 12px;
    padding: 24px;
    border-left: 4px solid #eab308;
  }
  
  .answer-text {
    font-size: 16px;
    line-height: 1.8;
    color: var(--gray-800);
    white-space: pre-wrap;
  }
}

// 空状态样式
.empty-state {
  padding: 40px 20px;
}

.loading-state {
  padding: 20px;
}

// 结果盒子样式
.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}

.result-answer {
  font-size: 15px;
  line-height: 1.6;
  color: var(--gray-800);
  margin: 0 0 8px 0;
  white-space: pre-wrap;
  word-break: break-word;
  &.is-error {
    color: var(--el-color-danger);
  }
}

.reasoning-chains {
  margin-top: 20px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  
  h4 {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-800);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 2px solid #0ea5e9;
  }
  
  .chain-group {
    margin-bottom: 16px;
  }
  
  .chain-path {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 12px;
    background: #f8fafc;
    border-radius: 8px;
    margin-bottom: 8px;
  }
  
  .chain-item {
    display: inline-flex;
    align-items: center;
  }
  
  .entity {
    display: inline-block;
    padding: 6px 14px;
    background: linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%);
    color: white;
    border-radius: 20px;
    font-size: 14px;
    font-weight: 500;
    box-shadow: 0 2px 4px rgba(14, 165, 233, 0.3);
  }
  
  .relation {
    color: #6b7280;
    font-size: 14px;
    font-weight: 600;
    margin: 0 4px;
  }
  
  .relation-arrow {
    color: #0ea5e9;
    font-size: 16px;
    font-weight: 700;
    margin: 0 4px;
  }
}

.viz-html {
  margin-top: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  overflow: auto;
  max-height: 480px;
  padding: 12px;
  background: #fff;
}

.viz-placeholder {
  font-size: 13px;
  color: var(--gray-500);
  margin-top: 8px;
}

.reasoning-logs {
  margin-top: 16px;
  
  .log-item {
    margin-bottom: 16px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    overflow: hidden;
    
    .log-header {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 16px;
      background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
      border-bottom: 1px solid #e5e7eb;
      
      .log-step {
        padding: 4px 12px;
        background: #0ea5e9;
        color: white;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
        white-space: nowrap;
      }
      
      .log-message {
        font-size: 14px;
        font-weight: 500;
        color: var(--gray-800);
      }
    }
  background: white;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  
  h4 {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-800);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 2px solid #0ea5e9;
  }
}
}

.graph-container {
  width: 100%;
  height: 500px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fafafa;
}

@media (max-width: 768px) {
  .answer-view {
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
