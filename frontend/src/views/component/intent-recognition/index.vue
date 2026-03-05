<script setup>
import { ref, onMounted } from 'vue'
import { Aim } from '@element-plus/icons-vue'
import { intentRecognitionHealthCheck, intentRecognitionRecognize } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')
const inputText = ref('')
const result = ref(null)

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
    healthDetail.value = '请检查后端并配置 algorithm/intent_recognition_model（Milvus、DeepSeek、BGE 模型等）'
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
  try {
    const { data } = await intentRecognitionRecognize({ text })
    result.value = data?.data ?? data
    ElMessage.success('识别完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '识别失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="intent-view">
    <div class="intent-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="intent-inner">
      <h1 class="page-title">
        <span class="title-highlight">意图识别</span>
      </h1>
      <p class="page-desc">
        对用户输入进行意图分类，结合 Milvus 检索与 DeepSeek 大模型，输出最匹配的意图（查询、故障诊断、规范查询等）。
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
          <span>意图识别</span>
        </template>
        <el-form label-width="100px" class="intent-form">
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
              :loading="loading"
              :icon="Aim"
              @click="submit"
            >
              识别意图
            </el-button>
          </el-form-item>
        </el-form>

        <div v-if="result" class="result-box">
          <p class="result-label">识别结果</p>
          <div class="result-fields">
            <p v-if="result.intent_name"><strong>意图名称：</strong>{{ result.intent_name }}</p>
            <p v-if="result.intent_description"><strong>意图描述：</strong>{{ result.intent_description }}</p>
            <p v-if="result.user_goal"><strong>用户目标：</strong>{{ result.user_goal }}</p>
            <p v-if="result.intent_summary"><strong>意图摘要：</strong>{{ result.intent_summary }}</p>
            <p v-if="result.domain"><strong>业务域：</strong>{{ result.domain }}</p>
            <p v-if="result.intent_id"><strong>意图 ID：</strong>{{ result.intent_id }}</p>
            <p v-if="result.core_keywords?.length"><strong>核心关键词：</strong>{{ result.core_keywords.join('、') }}</p>
            <p v-if="result.sample_utterances?.length"><strong>典型表述：</strong>{{ result.sample_utterances.slice(0, 3).join('；') }}</p>
          </div>
          <el-collapse>
            <el-collapse-item title="查看完整 JSON" name="json">
              <pre class="result-json">{{ JSON.stringify(result, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.intent-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.intent-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.intent-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.intent-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.intent-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.intent-inner {
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

.intent-form {
  max-width: 640px;
}

.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-label {
  font-weight: 600;
  margin-bottom: 12px;
  color: var(--gray-800);
}
.result-fields p {
  margin: 6px 0;
  font-size: 14px;
  color: var(--gray-700);
}
.result-json {
  font-size: 12px;
  overflow: auto;
  max-height: 280px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 768px) {
  .intent-view {
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
