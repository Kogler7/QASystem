<script setup>
import { ref, onMounted, watch, computed, nextTick } from 'vue'
import { MagicStick, Loading, UploadFilled, Download } from '@element-plus/icons-vue'
import { nl2cypherHealthCheck, nl2cypherGenerate } from '@/api'
import { nl2cypherSplitCharacter, nl2cypherSplitRecursive, nl2cypherSplitMarkdown, nl2cypherSplitPython, nl2cypherText2vector, nl2cypherGetVectors } from '@/api'
import { mutiRetriever } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

// 当前选中的主菜单与子菜单
const activeFeature = ref('cypher')
const activeSubFeature = ref('')

// 向量可视化
const loadingVectors = ref(false)
const vectors = ref([])
const hoveredVector = ref(null)
const hoverPosition = ref({ x: 0, y: 0 })
const hoverTimeout = ref(null)
const vectorStats = ref(null)
const visualizationConfig = ref({
  pointSize: 8,
  showLabels: true
})

// Canvas 引用
const canvasWrapper = ref(null)
const vectorCanvas = ref(null)

// 表单数据
const form = ref({
  question: '',
  graph_schema: `Graph schema: Relevant node labels and their properties are:
Article
Keyword
Author
Entity`,
  splitText: ''
})

// 文件上传相关
const fileInput = ref(null)
const currentFile = ref(null)
const fileName = ref('')

// 结果数据
const resultCypher = ref('')
const splitResult = ref(null)
const vectorResult = ref('')
//多源数据检索
const loadingMuti = ref(false)
const multiResults = ref([])
const multiStats = ref(null)


// 菜单项
const menuItems = [
  { key: 'cypher', label: '自然语言转Cypher', tooltip: '输入自然语言, 自动生成符合图谱的 Cypher 查询' },
  {
    key: 'split',
    label: '文本切片',
    tooltip: '四种文本切片方式的入口',
    children: [
      { key: 'split-char', label: '按字符切分', tooltip: '按照字符分割文本，适合简单分段' },
      { key: 'split-recursive', label: '递归切分', tooltip: '递归地按字符分片，可生成更小的段落' },
      { key: 'split-markdown', label: 'Markdown 切分', tooltip: '基于 Markdown 语法进行智能切片' },
      { key: 'split-python', label: 'Python 切分', tooltip: '对 Python 源码进行语言感知的切片' },
    ],
  },
  { key: 'vector', label: '文本向量化与存储', tooltip: '将文本向量化并写入数据库以备后续检索' },
  { key: 'visualize', label: '向量可视化', tooltip: '查看向量数据库的可视化展示' },
  { key: 'multi-retrieve', label: '多源数据检索', tooltip: '从JSON文件中检索节点对应的向量内容' },
]

// 工具提示映射
const tooltips = {}
menuItems.forEach(item => {
  tooltips[item.key] = item.tooltip
  if (item.children) {
    item.children.forEach(c => {
      tooltips[c.key] = c.tooltip
    })
  }
})

// 计算属性
const vectorDataAvailable = computed(() => {
  return healthStatus.value === 'ok'
})

const uniqueClusters = computed(() => {
  if (!vectorStats.value?.clusterCounts) return []
  return Object.keys(vectorStats.value.clusterCounts)
})

const clusterCounts = computed(() => {
  return vectorStats.value?.clusterCounts || {}
})

// 监听功能切换
watch(activeFeature, (newVal) => {
  form.value.splitText = ''
  splitResult.value = null
  vectorResult.value = null
  resultCypher.value = ''
  currentFile.value = null
  fileName.value = ''
  if (newVal !== 'split') {
    activeSubFeature.value = ''
  } else {
    if (!activeSubFeature.value) {
      activeSubFeature.value = menuItems.find(i => i.key === 'split')?.children?.[0]?.key || ''
    }
  }
  if (newVal !== 'multi-retrieve') {
    multiResults.value = []
    multiStats.value = null
  }
})

// 健康检查
const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await nl2cypherHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端是否启动并安装 algorithm/NL_to_cypher 依赖，配置 Ollama'
  }
}

// 文件上传处理
const handleFileUpload = (event) => {
  const file = event.target.files[0]
  if (!file) return
  
  const validTypes = ['text/plain', 'text/markdown', 'text/x-markdown', 'application/json']
  const fileExtension = file.name.split('.').pop().toLowerCase()
  const validExtensions = ['txt', 'md', 'markdown', 'json']
  
  if (!validTypes.includes(file.type) && !validExtensions.includes(fileExtension)) {
    ElMessage.error('请上传 txt、md 或 json 格式的文件')
    return
  }
  
  if (file.size > 10 * 1024 * 1024) { // 10MB
    ElMessage.error('文件大小不能超过 10MB')
    return
  }
  
  currentFile.value = file
  fileName.value = file.name
  
  const reader = new FileReader()
  reader.onload = (e) => {
    const content = e.target.result
    
    // 如果是 JSON 文件，尝试格式化显示
    if (file.type === 'application/json' || fileExtension === 'json') {
      try {
        const jsonData = JSON.parse(content)
        form.value.splitText = JSON.stringify(jsonData, null, 2)
      } catch (err) {
        form.value.splitText = content
        ElMessage.warning('JSON 文件格式错误，已按文本处理')
      }
    } else {
      form.value.splitText = content
    }
    
    ElMessage.success(`已加载文件: ${file.name} (${formatFileSize(file.size)})`)
  }
  
  reader.onerror = () => {
    ElMessage.error('文件读取失败')
  }
  
  reader.readAsText(file, 'UTF-8')
}

// 清除文件
const clearFile = () => {
  currentFile.value = null
  fileName.value = ''
  form.value.splitText = ''
  if (fileInput.value) {
    fileInput.value.value = ''
  }
}

// 格式文件大小
const formatFileSize = (bytes) => {
  if (bytes === 0) return '0 Bytes'
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

// 下载切片结果为 JSON
const downloadSplitResult = () => {
  if (!splitResult.value) return
  
  const dataStr = JSON.stringify(splitResult.value, null, 2)
  const dataBlob = new Blob([dataStr], { type: 'application/json' })
  const url = URL.createObjectURL(dataBlob)
  const link = document.createElement('a')
  link.href = url
  
  // 生成文件名
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
  const method = activeSubFeature.value.replace('split-', '')
  link.download = `text_split_${method}_${timestamp}.json`
  
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
  
  ElMessage.success('切片结果已下载')
}

// 提交表单
const submit = async () => {
  const active = activeFeature.value === 'split' ? activeSubFeature.value : activeFeature.value
  
  // 检查是否有输入内容
  if (!form.value.splitText?.trim() && !currentFile.value) {
    ElMessage.warning('请输入文本或上传文件')
    return
  }
  
  if (active === 'cypher') {
    await submitCypher()
  } else if (active === 'split-char') {
    await submitSplitCharacter()
  } else if (active === 'split-recursive') {
    await submitSplitRecursive()
  } else if (active === 'split-markdown') {
    await submitSplitMarkdown()
  } else if (active === 'split-python') {
    await submitSplitPython()
  } else if (active === 'vector') {
    await submitVector()
  }
}

// 各种提交函数
const submitSplitRecursive = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本或上传文件')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitRecursive(text)
    splitResult.value = data
    ElMessage.success('递归字符切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '递归字符切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitSplitMarkdown = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本或上传文件')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitMarkdown(text)
    splitResult.value = data
    ElMessage.success('Markdown 切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Markdown 切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitSplitPython = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本或上传文件')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitPython(text)
    splitResult.value = data
    ElMessage.success('Python 切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Python 切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitCypher = async () => {
  const question = form.value.question?.trim()
  if (!question) {
    ElMessage.warning('请输入自然语言问题')
    return
  }
  loading.value = true
  resultCypher.value = ''
  try {
    const { data } = await nl2cypherGenerate({
      question,
      graph_schema: form.value.graph_schema?.trim() ?? ''
    })
    resultCypher.value = data?.cypher ?? ''
    ElMessage.success('Cypher 生成完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Cypher 生成失败')
    resultCypher.value = ''
  } finally {
    loading.value = false
  }
}

const submitSplitCharacter = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本或上传文件')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitCharacter(text)
    splitResult.value = data
    ElMessage.success('字符切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '字符切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitVector = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要向量化的文本或上传文件')
    return
  }
  loading.value = true
  vectorResult.value = null
  try {
    const { data } = await nl2cypherText2vector(text)
    vectorResult.value = '存储完成'
    ElMessage.success('向量化存储完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '向量化失败')
    vectorResult.value = null
  } finally {
    loading.value = false
  }
}

// 向量可视化相关函数
const loadVectors = async () => {
  loadingVectors.value = true
  vectors.value = []
  hoveredVector.value = null
  
  try {
    const { data } = await nl2cypherGetVectors()
    
    if (data?.vectors && Array.isArray(data.vectors) && data.vectors.length > 0) {
      vectors.value = data.vectors
      vectorStats.value = data.stats || null
      
      // 加载完成后自动绘制
      await nextTick()
      visualizeVectors()
      ElMessage.success(`已加载 ${vectors.value.length} 个向量`)
    } else {
      // 生成模拟数据用于演示
      vectors.value = generateMockVectors()
      vectorStats.value = computeVectorStats()
      await nextTick()
      visualizeVectors()
      ElMessage.success(`已加载模拟数据 (${vectors.value.length} 个向量)`)
    }
  } catch (err) {
    console.error('加载向量数据失败:', err)
    // 开发模式下使用模拟数据
    vectors.value = generateMockVectors()
    vectorStats.value = computeVectorStats()
    await nextTick()
    visualizeVectors()
    ElMessage.success(`已加载模拟数据 (${vectors.value.length} 个向量)`)
  } finally {
    loadingVectors.value = false
  }
}

// 生成模拟向量数据
const generateMockVectors = () => {
  const mockVectors = []
  const sampleTexts = [
    "这是第一个文本片段，用于测试向量可视化功能。",
    "自然语言处理是人工智能的重要分支。",
    "向量数据库可以高效存储和检索文本嵌入。",
    "深度学习模型能够理解语言的语义信息。",
    "RAG架构结合了检索和生成的优势。",
    "文本切片是构建知识库的基础步骤。",
    "语义搜索可以找到最相关的文档。",
    "向量化表示使得计算机能够理解文本。",
    "可视化帮助理解高维数据的结构。",
    "聚类算法可以发现数据中的自然分组。"
  ]
  
  for (let i = 0; i < 20; i++) {
    mockVectors.push({
      id: i,
      embedding: new Array(384).fill(0).map(() => Math.random() * 2 - 1),
      text: sampleTexts[i % sampleTexts.length] + ` 这是第${i + 1}个片段。`,
      metadata: { source: 'mock', index: i },
      dimension: 384,
      position: { 
        x: (Math.random() - 0.5) * 1.6,  // 限制在 [-0.8, 0.8] 范围内
        y: (Math.random() - 0.5) * 1.6
      },
      cluster: Math.floor(Math.random() * 3),
      similarity: 0.5 + Math.random() * 0.3
    })
  }
  return mockVectors
}

// 计算向量位置的范围
const calculatePositionRange = () => {
  if (vectors.value.length === 0) return { minX: 0, maxX: 1, minY: 0, maxY: 1 }
  
  let minX = Infinity, maxX = -Infinity
  let minY = Infinity, maxY = -Infinity
  
  vectors.value.forEach(vector => {
    if (vector.position) {
      minX = Math.min(minX, vector.position.x)
      maxX = Math.max(maxX, vector.position.x)
      minY = Math.min(minY, vector.position.y)
      maxY = Math.max(maxY, vector.position.y)
    }
  })
  
   if (minX === Infinity || maxX === -Infinity) {
    return { minX: -1, maxX: 1, minY: -1, maxY: 1 }
  }
  
  // 添加 20% 的边距，确保所有点都在可视范围内
  let paddingX = (maxX - minX) * 0.2
  let paddingY = (maxY - minY) * 0.2
  
  // 确保有最小范围
  if (maxX - minX < 0.1) {
    paddingX = 0.5
  }
  if (maxY - minY < 0.1) {
    paddingY = 0.5
  }
  
  return {
    minX: minX - paddingX,
    maxX: maxX + paddingX,
    minY: minY - paddingY,
    maxY: maxY + paddingY
  }
}

// 可视化向量
const visualizeVectors = () => {
  if (!vectorCanvas.value || vectors.value.length === 0) return
  
  const canvas = vectorCanvas.value
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  
  // 设置 canvas 尺寸
  if (canvasWrapper.value) {
    const rect = canvasWrapper.value.getBoundingClientRect()
    canvas.width = rect.width
    canvas.height = 450
  } else {
    canvas.width = 800
    canvas.height = 450
  }
  
  const width = canvas.width
  const height = canvas.height
  
  // 清空画布
  ctx.clearRect(0, 0, width, height)
  
  // 计算位置范围
  const { minX, maxX, minY, maxY } = calculatePositionRange()
  
  // 计算缩放和平移，确保所有点都在可视范围内
  const rangeX = Math.max(maxX - minX, 0.1)  // 避免除零
  const rangeY = Math.max(maxY - minY, 0.1)
  
  // 计算边距（10%的边距）
  const margin = 0.1
  const scaleX = width * (1 - 2 * margin) / rangeX
  const scaleY = height * (1 - 2 * margin) / rangeY
  
  // 使用较小的缩放比例以确保所有点都在可视范围内
  const scale = Math.min(scaleX, scaleY)
  
  // 绘制向量点
  vectors.value.forEach((vector, index) => {
    let x, y
    
    if (vector.position) {
      // 归一化坐标到 [0, 1] 范围内
      const normalizedX = (vector.position.x - minX) / rangeX
      const normalizedY = (vector.position.y - minY) / rangeY
      
      // 应用边距
      x = margin * width + normalizedX * (width * (1 - 2 * margin))
      y = margin * height + normalizedY * (height * (1 - 2 * margin))
    } else {
      x = Math.random() * width
      y = Math.random() * height
    }
    
    // 确保点在画布内
    x = Math.max(10, Math.min(width - 10, x))
    y = Math.max(10, Math.min(height - 10, y))
    
    // 根据聚类着色
    ctx.fillStyle = getVectorColor(vector)
    ctx.beginPath()
    ctx.arc(x, y, visualizationConfig.value.pointSize, 0, Math.PI * 2)
    ctx.fill()
    
    // 添加白色边框
    ctx.strokeStyle = 'white'
    ctx.lineWidth = 2
    ctx.stroke()
    
    // 添加标签
    if (visualizationConfig.value.showLabels) {
      ctx.fillStyle = '#333'
      ctx.font = 'bold 12px Arial'
      ctx.textAlign = 'center'
      ctx.fillText((index + 1).toString(), x, y + 4)
    }
  })
}

// 获取向量颜色
const getVectorColor = (vector) => {
  if (vector.cluster !== undefined) {
    return getClusterColor(vector.cluster)
  }
  
  // 根据相似度着色
  const similarity = vector.similarity || 0.5
  const hue = 240 - (similarity * 120)
  return `hsl(${hue}, 70%, 60%)`
}

// 获取聚类颜色
const getClusterColor = (cluster) => {
  const colors = [
    '#FF6B6B', '#4ECDC4', '#FFD166', '#06D6A0',
    '#118AB2', '#EF476F'
  ]
  return colors[cluster % colors.length] || '#999'
}

// 处理画布悬停
const handleCanvasHover = (event) => {
  if (!vectorCanvas.value || vectors.value.length === 0) return
  
  clearTimeout(hoverTimeout.value)
  
  const rect = vectorCanvas.value.getBoundingClientRect()
  const canvas = vectorCanvas.value
  const x = event.clientX - rect.left
  const y = event.clientY - rect.top
  
  const hoverRadius = visualizationConfig.value.pointSize + 5
  
  // 计算位置范围（与绘制时保持一致）
  const { minX, maxX, minY, maxY } = calculatePositionRange()
  const rangeX = Math.max(maxX - minX, 0.1)
  const rangeY = Math.max(maxY - minY, 0.1)
  const margin = 0.1
  
  // 查找悬停的向量
  for (let i = 0; i < vectors.value.length; i++) {
    const vector = vectors.value[i]
    
    if (!vector.position) continue
    
    // 计算点在画布上的位置（与绘制时使用相同的计算方式）
    const normalizedX = (vector.position.x - minX) / rangeX
    const normalizedY = (vector.position.y - minY) / rangeY
    const pointX = margin * canvas.width + normalizedX * (canvas.width * (1 - 2 * margin))
    const pointY = margin * canvas.height + normalizedY * (canvas.height * (1 - 2 * margin))
    
    const distance = Math.sqrt(Math.pow(x - pointX, 2) + Math.pow(y - pointY, 2))
    
    if (distance <= hoverRadius) {
      hoverTimeout.value = setTimeout(() => {
        hoveredVector.value = {
          ...vector,
          index: i,
          textPreview: vector.text?.substring(0, 150) + (vector.text?.length > 150 ? '...' : '')
        }
        hoverPosition.value = { 
          x: Math.min(event.clientX + 20, window.innerWidth - 320),
          y: Math.max(event.clientY - 10, 20)
        }
      }, 100)  // 减少延迟时间
      return
    }
  }
  
  hoveredVector.value = null
}

// 处理画布离开
const handleCanvasLeave = () => {
  clearTimeout(hoverTimeout.value)
  hoveredVector.value = null
}

// 计算统计信息
const computeVectorStats = () => {
  if (vectors.value.length === 0) return null
  
  const clusters = new Set(vectors.value.map(v => v.cluster).filter(c => c !== undefined))
  const dimensions = vectors.value[0]?.dimension || 384
  
  // 计算平均相似度
  const similarities = vectors.value.map(v => v.similarity || 0.5)
  const avgSimilarity = similarities.reduce((sum, val) => sum + val, 0) / similarities.length
  
  // 计算聚类计数
  const clusterCounts = {}
  vectors.value.forEach(v => {
    if (v.cluster !== undefined) {
      clusterCounts[v.cluster] = (clusterCounts[v.cluster] || 0) + 1
    }
  })
  
  return {
    total: vectors.value.length,
    dimensions,
    clusters: clusters.size,
    avgSimilarity,
    clusterCounts
  }
}

// 复制到剪贴板
const copyToClipboard = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch (err) {
    console.error('复制失败:', err)
    ElMessage.error('复制失败')
  }
}

// 重新绘制可视化
const redrawVisualization = () => {
  if (vectors.value.length > 0) {
    visualizeVectors()
  }
}

// 监听配置变化
watch(
  () => [visualizationConfig.value.pointSize, visualizationConfig.value.showLabels],
  redrawVisualization,
  { deep: true, immediate: false }
)

// 组件挂载
onMounted(() => {
  checkHealth()
})

//多源检索
const loadMultiRetrieve = async () => {
  loadingMuti.value = true
  multiResults.value = []
  multiStats.value = null
  
  try {
    const { data } = await mutiRetriever()
    
    if (data) {
      // 如果接口直接返回数组
      if (Array.isArray(data)) {
        multiResults.value = data
        multiStats.value = {
          total_nodes: data.length,
          nodes_with_vector_address: data.filter(item => item.vector_address).length,
          successful_retrievals: data.length,
          success_rate: '100%'
        }
      }
      
      ElMessage.success(`检索完成，共找到 ${multiResults.value.length} 条数据`)
    } else {
      ElMessage.error('检索失败，无数据返回')
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '检索失败')
  } finally {
    loadingMuti.value = false
  }
}
</script>

<template>
  <div class="nl2cypher-view">
    <div class="nl2cypher-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <!-- 顶部标题与健康状态固定区域 -->
    <div class="nl2cypher-header">
      <div class="header-left"></div>
      <div class="header-right">
        <h1 class="page-title">
          <span class="title-highlight">文本向量化与切片组件库</span>
        </h1>
        <p class="page-desc">
          将文本转为向量存储、不同逻辑下的文本切片、自然语言转Cypher。
        </p>
        <div v-if="healthStatus !== null" class="health-row">
          <span class="health-label">服务状态：</span>
          <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
          <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
            <el-tag type="danger" size="small">不可用</el-tag>
          </el-tooltip>
          <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
        </div>
        <!-- 新增组件功能说明卡片 -->
        <el-card class="info-card" shadow="hover">
          <template #header>
            <span>组件功能说明</span>
          </template>
          <div class="info-content">
            <div class="section">
              <h4>功能概述</h4>
              <p>提供以下核心能力：</p>
              <ul>
                <li>文本按照多策略切片</li>
                <li>对切片结果生成向量并存储于数据库</li>
                <li>自然语言问题转换为 Cypher 查询</li>
                <li>向量数据库可视化展示</li>
              </ul>
            </div>
            <div class="section">
              <h4>使用流程</h4>
              <ol>
                <li>从左侧菜单选择所需功能（切片 / 向量化 / Cypher）</li>
                <li>输入文本或上传文件（txt/md/json）</li>
                <li>点击提交并查看结果</li>
                <li>切片结果可下载为 JSON 文件</li>
                <li>向量化操作会在后台写入数据库，可用于后续检索</li>
              </ol>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <!-- 主区域：左侧菜单 右侧动态内容 -->
    <div class="nl2cypher-inner">
      <!-- 侧边菜单 -->
      <div class="nl2cypher-menu">
        <template v-for="item in menuItems" :key="item.key">
          <div
            class="menu-entry"
            :class="{ active: activeFeature === item.key }"
            @click="activeFeature = item.key"
          >
            <el-tooltip :content="tooltips[item.key]" placement="right">
              <span>{{ item.label }}</span>
            </el-tooltip>
          </div>

          <!-- 子菜单显示在文本切片下面 -->
          <div
            v-if="item.children && activeFeature === item.key"
            class="submenu"
          >
            <div
              v-for="child in item.children"
              :key="child.key"
              class="menu-entry sub-entry"
              :class="{ active: activeSubFeature === child.key }"
              @click.stop="activeSubFeature = child.key"
            >
              <el-tooltip :content="tooltips[child.key]" placement="right">
                <span>{{ child.label }}</span>
              </el-tooltip>
            </div>
          </div>
        </template>
      </div>

      <div class="nl2cypher-content">
        <!-- 生成 Cypher 内容区（保持不变） -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'cypher'">
          <template #header>
            <span>生成 Cypher</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="自然语言问题" required>
              <el-input
                v-model="form.question"
                type="textarea"
                :rows="3"
                placeholder="例如：找出图中所有 Article 节点的总数。"
              />
            </el-form-item>
            <el-form-item label="图谱 Schema">
              <el-input
                v-model="form.graph_schema"
                type="textarea"
                :rows="6"
                placeholder="节点标签与属性描述，用于约束生成的 Cypher。"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                生成 Cypher
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="resultCypher" class="result-box">
            <div class="result-header">
              <p class="result-label">生成的 Cypher：</p>
              <el-button type="primary" size="small" @click="copyToClipboard(resultCypher)" text>
                复制
              </el-button>
            </div>
            <pre class="result-cypher">{{ resultCypher }}</pre>
          </div>
        </el-card>

        <!-- 字符切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-char'">
          <template #header>
            <span>字符切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入方式">
              <div class="input-method-selector">
                <div class="file-upload-area">
                  <input
                    ref="fileInput"
                    type="file"
                    accept=".txt,.md,.markdown,.json"
                    @change="handleFileUpload"
                    style="display: none"
                  />
                  <el-button
                    type="primary"
                    :icon="UploadFilled"
                    @click="fileInput?.click()"
                    size="small"
                  >
                    上传文件
                  </el-button>
                  <span class="file-info" v-if="fileName">
                    {{ fileName }}
                    <el-button
                      type="danger"
                      text
                      size="small"
                      @click="clearFile"
                    >
                      清除
                    </el-button>
                  </span>
                  <span class="file-hint" v-else>
                    支持 txt, md, json 格式，最大 10MB
                  </span>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="或输入文本">
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容，或上传文件..."
                resize="vertical"
              />
            </el-form-item>
            
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <div class="result-actions">
                <el-button 
                  type="primary" 
                  size="small" 
                  @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                  text
                >
                  复制JSON
                </el-button>
                <el-button 
                  type="success" 
                  size="small" 
                  :icon="Download"
                  @click="downloadSplitResult"
                  text
                >
                  下载结果
                </el-button>
              </div>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- 递归字符切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-recursive'">
          <template #header>
            <span>递归字符切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入方式">
              <div class="input-method-selector">
                <div class="file-upload-area">
                  <input
                    ref="fileInput"
                    type="file"
                    accept=".txt,.md,.markdown,.json"
                    @change="handleFileUpload"
                    style="display: none"
                  />
                  <el-button
                    type="primary"
                    :icon="UploadFilled"
                    @click="fileInput?.click()"
                    size="small"
                  >
                    上传文件
                  </el-button>
                  <span class="file-info" v-if="fileName">
                    {{ fileName }}
                    <el-button
                      type="danger"
                      text
                      size="small"
                      @click="clearFile"
                    >
                      清除
                    </el-button>
                  </span>
                  <span class="file-hint" v-else>
                    支持 txt, md, json 格式，最大 10MB
                  </span>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="或输入文本">
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容，或上传文件..."
                resize="vertical"
              />
            </el-form-item>
            
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <div class="result-actions">
                <el-button 
                  type="primary" 
                  size="small" 
                  @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                  text
                >
                  复制JSON
                </el-button>
                <el-button 
                  type="success" 
                  size="small" 
                  :icon="Download"
                  @click="downloadSplitResult"
                  text
                >
                  下载结果
                </el-button>
              </div>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- Markdown 切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-markdown'">
          <template #header>
            <span>Markdown 切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入方式">
              <div class="input-method-selector">
                <div class="file-upload-area">
                  <input
                    ref="fileInput"
                    type="file"
                    accept=".txt,.md,.markdown,.json"
                    @change="handleFileUpload"
                    style="display: none"
                  />
                  <el-button
                    type="primary"
                    :icon="UploadFilled"
                    @click="fileInput?.click()"
                    size="small"
                  >
                    上传文件
                  </el-button>
                  <span class="file-info" v-if="fileName">
                    {{ fileName }}
                    <el-button
                      type="danger"
                      text
                      size="small"
                      @click="clearFile"
                    >
                      清除
                    </el-button>
                  </span>
                  <span class="file-hint" v-else>
                    支持 txt, md, json 格式，最大 10MB
                  </span>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="或输入文本">
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容，或上传文件..."
                resize="vertical"
              />
            </el-form-item>
            
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <div class="result-actions">
                <el-button 
                  type="primary" 
                  size="small" 
                  @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                  text
                >
                  复制JSON
                </el-button>
                <el-button 
                  type="success" 
                  size="small" 
                  :icon="Download"
                  @click="downloadSplitResult"
                  text
                >
                  下载结果
                </el-button>
              </div>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- Python 切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-python'">
          <template #header>
            <span>Python 切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入方式">
              <div class="input-method-selector">
                <div class="file-upload-area">
                  <input
                    ref="fileInput"
                    type="file"
                    accept=".txt,.md,.markdown,.json,.py"
                    @change="handleFileUpload"
                    style="display: none"
                  />
                  <el-button
                    type="primary"
                    :icon="UploadFilled"
                    @click="fileInput?.click()"
                    size="small"
                  >
                    上传文件
                  </el-button>
                  <span class="file-info" v-if="fileName">
                    {{ fileName }}
                    <el-button
                      type="danger"
                      text
                      size="small"
                      @click="clearFile"
                    >
                      清除
                    </el-button>
                  </span>
                  <span class="file-hint" v-else>
                    支持 txt, md, json, py 格式，最大 10MB
                  </span>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="或输入文本">
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容，或上传文件..."
                resize="vertical"
              />
            </el-form-item>
            
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <div class="result-actions">
                <el-button 
                  type="primary" 
                  size="small" 
                  @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                  text
                >
                  复制JSON
                </el-button>
                <el-button 
                  type="success" 
                  size="small" 
                  :icon="Download"
                  @click="downloadSplitResult"
                  text
                >
                  下载结果
                </el-button>
              </div>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- 文本向量化功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'vector'">
          <template #header>
            <span>向量化并存储</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入方式">
              <div class="input-method-selector">
                <div class="file-upload-area">
                  <input
                    ref="fileInput"
                    type="file"
                    accept=".txt,.md,.markdown,.json"
                    @change="handleFileUpload"
                    style="display: none"
                  />
                  <el-button
                    type="primary"
                    :icon="UploadFilled"
                    @click="fileInput?.click()"
                    size="small"
                  >
                    上传文件
                  </el-button>
                  <span class="file-info" v-if="fileName">
                    {{ fileName }}
                    <el-button
                      type="danger"
                      text
                      size="small"
                      @click="clearFile"
                    >
                      清除
                    </el-button>
                  </span>
                  <span class="file-hint" v-else>
                    支持 txt, md, json 格式，最大 10MB
                  </span>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="或输入文本">
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要向量化的文本内容，或上传文件..."
                resize="vertical"
              />
            </el-form-item>
            
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始向量化
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="vectorResult" class="result-box">
            <div class="result-header">
              <p class="result-label">{{ vectorResult }}</p>
            </div>
          </div>
        </el-card>

        <!-- 向量可视化功能（保持不变） -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'visualize'">
          <template #header>
            <span>向量可视化</span>
          </template>
          <div class="visualization-container">
            <div class="visualization-controls">
              <el-button
                type="primary"
                :loading="loadingVectors"
                @click="loadVectors"
                :disabled="!vectorDataAvailable"
              >
                加载向量数据
              </el-button>
              <div class="control-group">
                <div class="slider-with-label">
                  <span class="slider-label">点大小: {{ visualizationConfig.pointSize }}</span>
                  <el-slider
                    v-model="visualizationConfig.pointSize"
                    :min="2"
                    :max="20"
                    :step="1"
                    show-stops
                    style="width: 200px;"
                    @input="redrawVisualization"
                  />
                </div>
                <el-tag type="info" size="small">使用 PCA 降维</el-tag>
              </div>
            </div>

            <div class="visualization-main">
              <div v-if="loadingVectors" class="loading-container">
                <el-icon class="is-loading" :size="30"><Loading /></el-icon>
                <p>正在加载向量数据...</p>
              </div>
              
              <div v-else-if="vectors.length > 0" class="visualization-canvas-container">
                <div class="canvas-wrapper" ref="canvasWrapper">
                  <canvas 
                    ref="vectorCanvas" 
                    class="vector-canvas"
                    @mousemove="handleCanvasHover"
                    @mouseleave="handleCanvasLeave"
                  ></canvas>
                  
                  <!-- 悬停提示框 -->
                  <div
                    v-if="hoveredVector"
                    class="tooltip"
                    :style="{
                      left: hoverPosition.x + 'px',
                      top: hoverPosition.y + 'px'
                    }"
                  >
                    <div class="tooltip-content">
                      <h4>文本片段 {{ hoveredVector.index + 1 }}</h4>
                      <div class="tooltip-text">
                        {{ hoveredVector.textPreview }}
                      </div>
                      <div class="tooltip-meta">
                        <span>向量维度: {{ hoveredVector.dimension }}</span>
                        <span>相似度: {{ (hoveredVector.similarity || 0).toFixed(3) }}</span>
                      </div>
                    </div>
                  </div>
                </div>
                
                <div class="visualization-legend" v-if="uniqueClusters.length > 0">
                  <div class="legend-item" v-for="cluster in uniqueClusters" :key="cluster">
                    <span class="legend-color" :style="{ backgroundColor: getClusterColor(cluster) }"></span>
                    <span class="legend-label">簇 {{ cluster }}</span>
                    <span class="legend-count">({{ clusterCounts[cluster] || 0 }})</span>
                  </div>
                </div>
              </div>
              
              <div v-else class="empty-state">
                <el-empty description="暂无向量数据">
                  <p>请先加载向量数据库</p>
                </el-empty>
              </div>
            </div>

            <div v-if="vectorStats" class="vector-stats">
              <el-row :gutter="20">
                <el-col :span="6">
                  <div class="stat-card">
                    <div class="stat-value">{{ vectorStats.total }}</div>
                    <div class="stat-label">总向量数</div>
                  </div>
                </el-col>
                <el-col :span="6">
                  <div class="stat-card">
                    <div class="stat-value">{{ vectorStats.dimensions }}</div>
                    <div class="stat-label">向量维度</div>
                  </div>
                </el-col>
                <el-col :span="6">
                  <div class="stat-card">
                    <div class="stat-value">{{ vectorStats.clusters }}</div>
                    <div class="stat-label">聚类数</div>
                  </div>
                </el-col>
                <el-col :span="6">
                  <div class="stat-card">
                    <div class="stat-value">{{ vectorStats.avgSimilarity.toFixed(3) }}</div>
                    <div class="stat-label">平均相似度</div>
                  </div>
                </el-col>
              </el-row>
            </div>
          </div>
        </el-card>
        <!--多源数据检索-->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'multi-retrieve'">
        <template #header>
          <span>多源数据检索</span>
        </template>
        <!-- 新增链接区域 -->
        <div style="margin-bottom: 20px; text-align: center;">
          <div style="margin-bottom: 15px; display: flex; gap: 20px; justify-content: center;">
            <el-link 
              type="primary" 
              :underline="false" 
              href="http://localhost:7474/browser/" 
              target="_blank"
              style="padding: 8px 16px; border: 1px solid #409eff; border-radius: 4px;"
            >
              图数据库管理
            </el-link>
            <el-link 
              type="success" 
              :underline="false" 
              href="http://10.126.62.88:5601/app/enterprise_search/elasticsearch" 
              target="_blank"
              style="padding: 8px 16px; border: 1px solid #67c23a; border-radius: 4px;"
            >
              文档数据库管理
            </el-link>
          </div>
        </div>
        <!-- 控制按钮 -->
        <div style="margin-bottom: 20px; text-align: center;">
          <el-button
            type="primary"
            :loading="loadingMuti"
            :icon="MagicStick"
            @click="loadMultiRetrieve"
          >
            查找向量库
          </el-button>
        </div>
        
        <!-- 加载状态 -->
        <div v-if="loadingMuti" class="loading-container" style="text-align: center; padding: 20px;">
          <el-icon class="is-loading" :size="30"><Loading /></el-icon>
          <p>正在检索数据...</p>
        </div>
        
        <!-- 结果展示 -->
        <div v-if="multiResults.length > 0">
          <!-- 统计信息 -->
          <div v-if="multiStats" style="margin-bottom: 20px; background: #f5f7fa; padding: 15px; border-radius: 4px;">
            <h4 style="margin: 0 0 10px 0; color: #333;">统计信息</h4>
            <div style="display: flex; gap: 20px; flex-wrap: wrap;">
              <div>
                <span style="color: #666;">总节点数: </span>
                <strong style="color: #409eff;">{{ multiStats.total_nodes }}</strong>
              </div>
              <div>
                <span style="color: #666;">有向量地址: </span>
                <strong style="color: #409eff;">{{ multiStats.nodes_with_vector_address }}</strong>
              </div>
              <div>
                <span style="color: #666;">成功检索: </span>
                <strong style="color: #67c23a;">{{ multiStats.successful_retrievals }}</strong>
              </div>
              <div>
                <span style="color: #666;">成功率: </span>
                <strong style="color: #e6a23c;">{{ multiStats.success_rate }}</strong>
              </div>
            </div>
          </div>
          
          <!-- 结果列表 -->
          <div class="result-box" style="margin-top: 20px;">
            <div class="result-header">
              <p class="result-label">检索结果 (共 {{ multiResults.length }} 个):</p>
            </div>
            
            <div class="results-list">
              <div v-for="(result, index) in multiResults" :key="index" class="result-item">
                <div style="margin-bottom: 10px;">
                  <span style="color: #409eff; font-weight: bold;">节点 {{ result.node_id }}</span>
                  <span v-if="result.node_name" style="color: #333; margin-left: 10px;">
                    ({{ result.node_name }})
                  </span>
                </div>
                
                <div style="margin: 8px 0;">
                  <span style="color: #666;">标签: </span>
                  <span style="color: #333;">
                    {{ result.labels.join(', ') }}
                  </span>
                </div>
                
                <div style="margin: 8px 0;">
                  <span style="color: #666;">向量地址: </span>
                  <span style="color: #e6a23c; font-family: monospace;">
                    {{ result.vector_address }}
                  </span>
                </div>
                
                <div style="margin: 8px 0;">
                  <span style="color: #666;">切片编号: </span>
                  <span style="color: #333; font-weight: bold;">
                    {{ result.slice_number }}
                  </span>
                </div>
                
                <div style="margin: 8px 0;">
                  <span style="color: #666;">向量内容: </span>
                  <div style="color: #333; background: #f8f9fa; padding: 10px; 
                           border-radius: 4px; margin-top: 5px; border-left: 3px solid #409eff;">
                    {{ result.vector_content.length > 200 ? result.vector_content.substring(0, 200) + '...' : result.vector_content }}
                    <span v-if="result.vector_content.length > 200" style="color: #909399; font-size: 12px;">
                      (共 {{ result.vector_content.length }} 字符)
                    </span>
                  </div>
                </div>
                
                <div v-if="result.content_length" style="margin: 8px 0; text-align: right;">
                  <span style="color: #909399; font-size: 12px;">
                    内容长度: {{ result.content_length }} 字符
                  </span>
                </div>
                
                <!-- 分隔线 -->
                <el-divider v-if="index < multiResults.length - 1" style="margin: 20px 0;" />
              </div>
            </div>
          </div>
        </div>
        
        <!-- 无数据状态 -->
        <div v-else-if="!loadingMuti" style="text-align: center; padding: 40px 0;">
          <el-empty description="点击上方按钮开始检索" />
        </div>
      </el-card>
        <!-- 其他功能保持占位或说明 -->
        <el-card class="form-card" shadow="hover" v-if="
          activeFeature === 'split' && !activeSubFeature ||
          (activeFeature !== 'cypher' && activeFeature !== 'split' && activeFeature !== 'vector' && activeFeature !== 'visualize'&& activeFeature !== 'multi-retrieve')
        ">
          <template #header>
            <span v-if="activeFeature === 'split'">
              文本切片
            </span>
            <span v-else-if="activeFeature === 'vector'">
              文本向量化与存储
            </span>
            <span v-else>
              占位功能
            </span>
          </template>
          <div class="placeholder-box">
            <p v-if="activeFeature === 'split'">
              请选择左侧子菜单中的一种切片方式。
            </p>
            <p v-else>
              该界面尚未实现具体逻辑，仅作为占位。
            </p>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.nl2cypher-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.nl2cypher-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.nl2cypher-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.nl2cypher-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.nl2cypher-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

@keyframes float {
  0%, 100% { transform: translateY(0) scale(1); }
  50% { transform: translateY(-20px) scale(1.05); }
}

.nl2cypher-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  max-width: var(--content-max-width);
  margin: 0 auto 24px;
  padding: 0 0 24px;
  border-bottom: 1px solid var(--gray-200);
}
.nl2cypher-header .header-right {
  text-align: left;
  flex: 1;
}

/* 新增信息卡片样式 */
.info-card {
  margin: 16px 16px 16px 16px;
  border: 1px solid #007bff;
  padding: 8px 8px 8px 8px;
  border-radius: 6px;
}
.info-card .el-card__header,
.info-card :deep(.el-card__header) {
  color: #44aff6 !important;
  font-weight: 600;
  font-size: 18px;
  margin-top: -10px;
  margin-bottom: -26px;
}
.info-content {
  font-size: 14px;
  margin-top: 0px;
  margin-bottom: -20px;
  color: var(--gray-700);
}
.info-content .section {
  margin-bottom: 10px;
}
.info-content h4 {
  margin: 0 0 4px;
  font-size: 15px;
  color: var(--gray-900);
  font-weight: 600;
}
.info-content ul,
.info-content ol {
  margin: 4px 0 0 20px;
}
.info-content li {
  margin: 2px 0;
}

.nl2cypher-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
  display: flex;
  gap: 24px;
}
.nl2cypher-menu {
  width: 160px;
  flex-shrink: 0;
}

.menu-entry {
  padding: 8px 12px;
  border-radius: 4px;
  cursor: pointer;
  transition: background-color 0.2s, color 0.2s;
  margin-bottom: 4px;
  font-size: 14px;
  color: var(--gray-700);
}
.menu-entry:hover {
  background-color: var(--gray-100);
  color: var(--primary);
}
.menu-entry.active {
  background-color: var(--primary-light);
  color: #258bde;
  font-weight: 500;
}
.nl2cypher-content {
  flex: 1;
  min-width: 0;
}

.page-title {
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 2px;
  color: var(--gray-900);
}
.page-desc {
  font-size: 15px;
  color: var(--gray-600);
  margin-bottom: 4px;
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
  margin-bottom: 24px;
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.nl2cypher-form {
  max-width: 720px;
  
  .input-method-selector {
    width: 100%;
    
    .file-upload-area {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 12px;
      border: 1px dashed var(--gray-300);
      border-radius: 4px;
      background: var(--gray-50);
      
      .file-info {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: white;
        border-radius: 4px;
        border: 1px solid var(--gray-200);
        font-size: 13px;
        color: var(--gray-700);
      }
      
      .file-hint {
        font-size: 12px;
        color: var(--gray-500);
        text-align: center;
      }
    }
  }
}

.result-box {
  margin-top: 20px;
  padding: 20px;
  background: var(--gray-50);
  border-radius: 8px;
  border: 1px solid var(--gray-200);
}
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  .result-label {
    font-weight: 600;
    margin: 0;
    color: var(--gray-800);
    font-size: 15px;
  }
  
  .result-actions {
    display: flex;
    gap: 8px;
  }
}
.result-cypher, .result-json {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  overflow: auto;
  max-height: 400px;
  padding: 12px;
  background: var(--gray-100);
  border-radius: 6px;
  border: 1px solid var(--gray-300);
}
.result-cypher {
  color: var(--primary-dark);
}
.result-json {
  color: var(--gray-700);
}

// 切片结果样式
.split-result {
  .result-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--gray-200);
    
    .download-btn {
      margin-left: auto;
    }
  }
  .chunks-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .chunk-item {
    background: white;
    border-radius: 6px;
    border: 1px solid var(--gray-200);
    overflow: hidden;
  }
  .chunk-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: var(--gray-50);
    border-bottom: 1px solid var(--gray-200);
  }
  .chunk-index {
    font-weight: 500;
    color: var(--gray-800);
    font-size: 13px;
  }
  .chunk-length {
    color: var(--gray-600);
    font-size: 12px;
  }
  .chunk-content {
    margin: 0;
    padding: 12px;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow: auto;
    background: var(--gray-50);
  }
}

@media (max-width: 768px) {
  .nl2cypher-view {
    padding: 24px 20px 60px;
  }
  .page-title {
    font-size: 22px;
  }
  .page-desc {
    font-size: 14px;
  }
  .nl2cypher-inner {
    flex-direction: column;
  }
  .nl2cypher-menu {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
    .menu-entry {
      flex: 1;
      min-width: 120px;
      text-align: center;
      margin-bottom: 0;
    }
  }
  .chunk-header {
    flex-wrap: wrap;
    gap: 8px;
  }
  
  .nl2cypher-form {
    .input-method-selector {
      .file-upload-area {
        .file-info {
          flex-direction: column;
          align-items: flex-start;
          gap: 4px;
        }
      }
    }
  }
}

/* submenu styling */
.submenu {
  margin-left: 16px;
  margin-top: 4px;
}
.sub-entry {
  padding-left: 24px;
  font-size: 13px;
}
.sub-entry.active {
  font-weight: 500;
}

/* 可视化样式 */
.visualization-container {
  .visualization-controls {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    padding: 16px;
    background: var(--gray-50);
    border-radius: 8px;
    
    .control-group {
      display: flex;
      align-items: center;
      gap: 20px;
    }
    
    .slider-with-label {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 4px;
      
      .slider-label {
        font-size: 12px;
        color: var(--gray-600);
        font-weight: 500;
      }
    }
  }
  
  .visualization-main {
    min-height: 500px;
    border: 1px solid var(--gray-200);
    border-radius: 8px;
    background: white;
    position: relative;
    margin-bottom: 20px;
    
    .loading-container {
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      height: 500px;
      color: var(--gray-600);
      
      p {
        margin-top: 10px;
      }
    }
    
    .visualization-canvas-container {
      position: relative;
      height: 500px;
      
      .canvas-wrapper {
        position: relative;
        height: 450px;
        overflow: hidden;
        cursor: crosshair;
        background: #f8f9fa;
        border-radius: 4px;
      }
      
      .vector-canvas {
        width: 100%;
        height: 100%;
        display: block;
      }
      
      .tooltip {
        position: fixed;
        background: white;
        border: 1px solid var(--gray-300);
        border-radius: 6px;
        padding: 12px;
        max-width: 300px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        z-index: 1000;
        pointer-events: none;
        
        .tooltip-content {
          h4 {
            margin: 0 0 8px 0;
            color: var(--gray-900);
            font-size: 14px;
          }
          
          .tooltip-text {
            max-height: 200px;
            overflow-y: auto;
            font-size: 12px;
            line-height: 1.4;
            color: var(--gray-700);
            margin-bottom: 8px;
          }
          
          .tooltip-meta {
            display: flex;
            justify-content: space-between;
            font-size: 11px;
            color: var(--gray-500);
            
            span {
              display: inline-block;
              margin-right: 8px;
            }
          }
        }
      }
    }
    
    .visualization-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      padding: 12px;
      border-top: 1px solid var(--gray-200);
      background: var(--gray-50);
      
      .legend-item {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        
        .legend-color {
          width: 12px;
          height: 12px;
          border-radius: 50%;
        }
        
        .legend-count {
          color: var(--gray-500);
        }
      }
    }
    
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 500px;
      color: var(--gray-600);
      
      .el-empty {
        .el-empty__description {
          p {
            margin-top: 8px;
            color: var(--gray-500);
            font-size: 13px;
          }
        }
      }
    }
  }
  
  .vector-stats {
    .stat-card {
      text-align: center;
      padding: 16px;
      background: var(--gray-50);
      border-radius: 6px;
      transition: transform 0.2s, box-shadow 0.2s;
      cursor: default;
      
      &:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
      }
      
      .stat-value {
        font-size: 24px;
        font-weight: 600;
        color: var(--primary);
        margin-bottom: 4px;
        transition: color 0.2s;
      }
      
      &:hover .stat-value {
        color: var(--primary-dark);
      }
      
      .stat-label {
        font-size: 12px;
        color: var(--gray-600);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
    }
  }
}
.results-list {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
  font-size: 14px;
  line-height: 1.5;
}

.result-item {
  margin-bottom: 20px;
}
</style>