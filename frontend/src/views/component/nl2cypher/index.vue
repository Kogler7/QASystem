<script setup>
import { ref, onMounted } from 'vue'
import { MagicStick } from '@element-plus/icons-vue'
import { nl2cypherHealthCheck, nl2cypherGenerate } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

const form = ref({
  question: '',
  graph_schema: `Graph schema: Relevant node labels and their properties are:
Article
Keyword
Author
Entity`
})

const resultCypher = ref('')

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

const submit = async () => {
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
    ElMessage.success('生成完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '生成失败')
    resultCypher.value = ''
  } finally {
    loading.value = false
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="nl2cypher-view">
    <div class="nl2cypher-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="nl2cypher-inner">
      <h1 class="page-title">
        <span class="title-highlight">向量转化存储与自然语言转 Cypher</span>
      </h1>
      <p class="page-desc">
        输入自然语言问题与图谱 Schema，由大模型生成可执行的 Cypher 查询；依赖 Ollama 本地模型。
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
          <p class="result-label">生成的 Cypher：</p>
          <pre class="result-cypher">{{ resultCypher }}</pre>
        </div>
      </el-card>
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

.nl2cypher-inner {
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

.nl2cypher-form {
  max-width: 720px;
}

.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-label {
  font-weight: 500;
  margin-bottom: 8px;
  color: var(--gray-800);
}
.result-cypher {
  font-family: ui-monospace, monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  overflow: auto;
  max-height: 320px;
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
}
</style>
