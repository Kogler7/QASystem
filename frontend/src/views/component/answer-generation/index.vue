<script setup>
import { ref, onMounted } from 'vue'
import { ChatDotRound, PictureFilled } from '@element-plus/icons-vue'
import {
  answerGenerationHealthCheck,
  answerGenerationAsk,
  answerGenerationAskVisualize
} from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const loadingViz = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')
const question = ref('')
const detailed = ref(false)
const result = ref(null)
const resultWithViz = ref(null)

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
    const { data } = await answerGenerationAsk({ question: q, detailed: detailed.value })
    result.value = data
    ElMessage.success(data?.success ? '回答完成' : '未得到有效答案')
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
  resultWithViz.value = null
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

onMounted(checkHealth)
</script>

<template>
  <div class="answer-view">
    <div class="answer-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="answer-inner">
      <h1 class="page-title">
        <span class="title-highlight">答案生成</span>
      </h1>
      <p class="page-desc">
        基于 Neo4j 知识图谱与问题求解器（问题定义→分解→方案获取），对自然语言问题生成答案，可选返回求解详情与路径可视化。
      </p>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">服务状态：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>提问</span>
        </template>
        <el-form label-width="100px" class="answer-form">
          <el-form-item label="问题" required>
            <el-input
              v-model="question"
              type="textarea"
              :rows="4"
              placeholder="输入基于知识图谱的自然语言问题，例如：列车制动系统常见故障有哪些？"
            />
          </el-form-item>
          <el-form-item label="返回求解详情">
            <el-switch v-model="detailed" />
            <span class="form-hint">开启后返回问题模型、子问题、方案模型等</span>
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
            <el-button
              :loading="loadingViz"
              :icon="PictureFilled"
              @click="submitWithVisualization"
            >
              答案 + 可视化
            </el-button>
          </el-form-item>
        </el-form>

        <div v-if="result" class="result-box">
          <p class="result-label">回答</p>
          <p class="result-answer" :class="{ 'is-error': !result.success }">
            {{ result.answer }}
          </p>
          <p v-if="result.error" class="result-error">{{ result.error }}</p>
          <el-collapse v-if="result.details">
            <el-collapse-item title="查看求解详情" name="details">
              <pre class="result-json">{{ JSON.stringify(result.details, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>

        <div v-if="resultWithViz" class="result-box">
          <p class="result-label">回答（含可视化）</p>
          <p class="result-answer" :class="{ 'is-error': !resultWithViz.success }">
            {{ resultWithViz.answer }}
          </p>
          <div
            v-if="resultWithViz.visualization"
            class="viz-html"
            v-html="resultWithViz.visualization"
          />
          <p v-else-if="resultWithViz.success && !resultWithViz.visualization" class="viz-placeholder">
            未生成可视化内容
          </p>
        </div>
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

.answer-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.answer-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.answer-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.answer-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
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

.form-card {
  border-radius: var(--card-radius-lg);
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.answer-form {
  max-width: 640px;
}
.form-hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--gray-500);
}

.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-label {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--gray-800);
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
.result-error {
  font-size: 13px;
  color: var(--el-color-danger);
  margin: 4px 0;
}
.result-json {
  font-size: 12px;
  overflow: auto;
  max-height: 320px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
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
