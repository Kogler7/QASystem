<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Aim, Document, Plus, Refresh } from '@element-plus/icons-vue'
import { intentRecognitionHealthCheck, intentRecognitionRecognize, intentRecognitionStream } from '@/api/modules/component/intent-recognition'
import { ElMessage } from 'element-plus'

const router = useRouter()

const subMenuItems = [
  {
    id: 'intent-recognition-simple',
    title: '基础意图识别',
    desc: '基础意图识别功能，对用户输入进行意图识别，直接显示答案，不提供详细过程。',
    icon: Aim,
    color: 'var(--primary-500)',
    route: 'intent_simple'
  },
  {
    id: 'intent-recognition-detail',
    title: '深度意图识别',
    desc: '详细版意图识别，呈现更详细的推理过程。',
    icon: Document,
    color: 'var(--primary-600)',
    route: 'deep_intent_recognition'
  },
  {
    id: 'intent-add',
    title: '意图新增',
    desc: '新增自定义意图功能，支持添加新的意图类型，扩展系统识别能力。',
    icon: Plus,
    color: 'var(--success-500)',
    route: 'intent-add'
  },
]

const recognitionType = ref('basic') // 'basic' 或 'deep'
const inputText = ref('')
const loading = ref(false)
const result = ref(null)
const streamingResult = ref('')
const isStreaming = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

const handleCardClick = (item) => {
  if (item.route) {
    router.push(`/component/intent-recognition/${item.route}`)
  } else {
    router.push(`/component/intent-recognition`)
  }
}

const handleBack = () => {
  router.push('/component')
}

const getSimilarityClass = (score) => {
  if (score >= 0.8) return 'high'
  if (score >= 0.6) return 'medium'
  return 'low'
}

// 格式化数组字段
const formatArrayField = (field, limit = null, separator = '、') => {
  if (!field) return ''
  
  // 如果是字符串，尝试解析为数组
  let arr = field
  if (typeof field === 'string') {
    try {
      // 移除首尾的单引号或双引号，然后解析
      const cleaned = field.replace(/^['"]|['"]$/g, '')
      arr = JSON.parse(cleaned)
    } catch (e) {
      try {
        // 尝试使用 eval 解析
        arr = eval(field)
      } catch (e2) {
        // 如果解析失败，返回原始字符串
        return field
      }
    }
  }
  
  // 如果是数组，处理并返回
  if (Array.isArray(arr)) {
    const items = limit ? arr.slice(0, limit) : arr
    return items.join(separator)
  }
  
  // 其他情况，返回原始值
  return field
}

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await intentRecognitionHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '意图识别模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端并配置 algorithm/intent_recognition_model（Milvus、BGE 模型等）'
  }
}

const submit = async () => {
  const text = inputText.value?.trim()
  if (!text) {
    ElMessage.warning('请输入待识别文本')
    return
  }
  
  loading.value = true
  result.value = null
  streamingResult.value = ''
  
  try {
    if (recognitionType.value === 'basic') {
      // 基础意图识别
      isStreaming.value = true
      const { data } = await intentRecognitionRecognize({ text })
      result.value = data?.data ?? data
      ElMessage.success('识别完成')
      isStreaming.value = false
    } else {
      // 深度意图识别（流式）
      isStreaming.value = true
      const response = await intentRecognitionStream({ text })
      
      if (!response.ok) {
        throw new Error('API 请求失败')
      }
      
      // 处理流式响应
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        
        const chunk = decoder.decode(value, { stream: true })
        // 解析 SSE 格式
        const lines = chunk.split('\n')
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data) {
              streamingResult.value += data
            }
          }
        }
      }
      ElMessage.success('识别完成')
    }
  } catch (error) {
    ElMessage.error(error?.message || '识别失败')
    if (recognitionType.value === 'deep') {
      streamingResult.value = '识别失败：' + error.message
    }
  } finally {
    loading.value = false
    isStreaming.value = false
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="intent-sub-view">
    <div class="intent-sub-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div v-if="$route.path === '/component/intent-recognition'" class="intent-sub-inner">
      <div class="header-actions">
        <h1 class="page-title">
          <span class="title-highlight">意图识别组件</span>
        </h1>
      </div>
      <p class="page-desc">
        意图识别相关子组件列表，包括基础意图识别，深度意图识别等。<br>
        基础意图识别：直接对用户输入进行意图识别，返回简单的答案。<br>
        深度意图识别：对用户输入进行详细的意图识别，展示推理过程和答案。
      </p>

      <!-- 意图识别功能 -->
      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>意图识别</span>
        </template>
        
        <div v-if="healthStatus !== null" class="health-row">
          <span class="health-label">服务状态：</span>
          <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
          <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
            <el-tag type="danger" size="small">不可用</el-tag>
          </el-tooltip>
          <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
        </div>
        
        <el-form label-width="100px" class="intent-form">
          <el-form-item label="识别类型" required>
            <el-select v-model="recognitionType" class="w-full">
              <el-option label="基础意图识别" value="basic" />
              <el-option label="深度意图识别" value="deep" />
            </el-select>
          </el-form-item>
          <el-form-item label="用户输入" required>
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="4"
              placeholder="例如：发现裂纹后应如何处理？电机部分需要检查哪些关键部位？"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :icon="Aim"
              @click="submit"
            >
              识别意图
            </el-button>
          </el-form-item>
        </el-form>

        <!-- 加载状态 -->
        <div v-if="isStreaming" class="streaming-indicator">
          <el-icon class="is-loading"><Refresh /></el-icon>
          正在识别...
        </div>

        <!-- 基础识别结果 -->
        <div v-if="recognitionType === 'basic' && result" class="result-box">
          <p class="result-label">识别结果</p>
          <div class="result-fields">
            <!-- 相关意图 -->
            <div v-if="result['相关意图']" class="result-section">
              <h4 class="section-title">相关意图</h4>
              <div v-if="Array.isArray(result['相关意图'])" class="intent-list">
                <div v-for="(intent, index) in result['相关意图']" :key="index" class="intent-item">
                  <div class="intent-header">
                    <span class="intent-badge">意图 {{ index + 1 }}</span>
                  </div>
                  <div class="intent-content">
                    <p v-if="intent.intent_id"><strong>ID：</strong>{{ intent.intent_id }}</p>
                    <p v-if="intent.intent_name_text || intent.intent_name"><strong>名称：</strong>{{ intent.intent_name_text || intent.intent_name }}</p>
                    <p v-if="intent.intent_description_text || intent.intent_description"><strong>描述：</strong>{{ intent.intent_description_text || intent.intent_description }}</p>
                    <p v-if="intent.domain"><strong>业务域：</strong>{{ intent.domain }}</p>
                    <p v-if="intent.status"><strong>状态：</strong>{{ intent.status }}</p>
                    <p v-if="intent.create_time"><strong>创建时间：</strong>{{ intent.create_time }}</p>
                    <p v-if="intent.update_time"><strong>更新时间：</strong>{{ intent.update_time }}</p>
                    <p v-if="intent.tags"><strong>标签：</strong>{{ formatArrayField(intent.tags) }}</p>
                    <p v-if="intent.core_keywords"><strong>关键词：</strong>{{ formatArrayField(intent.core_keywords) }}</p>
                    <p v-if="intent.sample_utterances"><strong>典型表述：</strong>{{ formatArrayField(intent.sample_utterances, 2, '；') }}</p>
                  </div>
                </div>
              </div>
              <div v-else class="intent-single">
                <p>{{ result['相关意图'] }}</p>
              </div>
            </div>
            
            <!-- 向量 -->
            <div v-if="result['向量']" class="result-section">
              <h4 class="section-title">向量</h4>
              <div class="vector-info">
                <p>{{ result['向量'] }}</p>
              </div>
            </div>
            
            <!-- 与原始文本的相似度 -->
            <div v-if="result['相似度']" class="result-section">
              <h4 class="section-title">相似度</h4>
              <div v-if="Array.isArray(result['相似度'])" class="similarity-list">
                <div v-for="(similarity, index) in result['相似度']" :key="index" class="similarity-item">
                  <div class="similarity-header">
                    <span class="similarity-badge">结果 {{ index + 1 }}</span>
                  </div>
                  <div class="similarity-content">
                    <p v-if="similarity['意图ID']">意图 ID：{{ similarity['意图ID'] }}</p>
                    <p v-if="similarity['稠密向量的相似度']">稠密向量的相似度：{{ similarity['稠密向量的相似度'] }}</p>
                    <p v-if="similarity['稀疏向量的相似度']">稀疏向量的相似度：{{ similarity['稀疏向量的相似度'] }}</p>
                    <p v-if="similarity['RRF分数']">RRF分数：{{ similarity['RRF分数'] }}</p>
                  </div>
                </div>
              </div>
              <div v-else class="similarity-single">
                <p>{{ result['与原始文本的相似度'] }}</p>
              </div>
            </div>
          </div>
          <el-collapse v-if="result['筛选的最终意图']">
            <el-collapse-item title="查看筛选的最终意图" name="json">
              <pre class="result-json">{{ JSON.stringify(result['筛选的最终意图'], null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>

        <!-- 深度识别结果 -->
        <div v-else-if="recognitionType === 'deep' && (streamingResult || isStreaming)" class="result-box">
          <p class="result-label">识别结果</p>
          <div class="streaming-output" v-html="streamingResult"></div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style src="./style.css" scoped></style>
