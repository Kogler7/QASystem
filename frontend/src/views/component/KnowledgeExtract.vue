<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Search, Document, Connection, List } from '@element-plus/icons-vue'
import { knowledgeExtract, extractHealthCheck } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null) // null | 'ok' | 'error'
const form = reactive({
  main_object: 'Term',
  text: '',
  use_local: true,
  use_templates: true,
  store_to_neo4j: false
})

const result = ref(null)

const submit = async () => {
  const { main_object, text } = form
  if (!main_object?.trim()) {
    ElMessage.warning('请输入主对象类型')
    return
  }
  if (!text?.trim()) {
    ElMessage.warning('请输入待抽取文本')
    return
  }
  loading.value = true
  result.value = null
  try {
    const { data } = await knowledgeExtract({
      main_object: main_object.trim(),
      text: text.trim(),
      use_local: form.use_local,
      use_templates: form.use_templates,
      store_to_neo4j: form.store_to_neo4j
    })
    result.value = data?.data ?? data
    ElMessage.success('抽取完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '抽取失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

const rawList = () => result.value?.raw ?? []
const graph = () => result.value?.graph ?? {}

const checkHealth = async () => {
  healthStatus.value = null
  try {
    await extractHealthCheck()
    healthStatus.value = 'ok'
  } catch {
    healthStatus.value = 'error'
  }
}
onMounted(checkHealth)
</script>

<template>
  <div class="extract-view">
    <div class="extract-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="extract-inner">
      <h1 class="page-title">
        <span class="title-highlight">知识抽取</span>
      </h1>
      <p class="page-desc">
        从文本中抽取指定类型的实体与关系，支持本地模型与模板，可选写入 Neo4j。
      </p>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">抽取服务：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tag v-else type="danger" size="small">不可用</el-tag>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>抽取参数</span>
        </template>
        <el-form :model="form" label-width="120px" class="extract-form">
          <el-form-item label="主对象类型" required>
            <el-input
              v-model="form.main_object"
              placeholder="如 Term、Fault、Rule 等"
              clearable
            />
          </el-form-item>
          <el-form-item label="待抽取文本" required>
            <el-input
              v-model="form.text"
              type="textarea"
              :rows="5"
              placeholder="输入需要抽取知识的文本，例如：故障现象：列车在江泰路联锁区运营停车点不能自动取消。"
            />
          </el-form-item>
          <el-form-item label="使用本地模型">
            <el-switch v-model="form.use_local" />
            <span class="form-hint">默认开启</span>
          </el-form-item>
          <el-form-item label="启用模板匹配">
            <el-switch v-model="form.use_templates" />
            <span class="form-hint">默认开启</span>
          </el-form-item>
          <el-form-item label="写入 Neo4j">
            <el-switch v-model="form.store_to_neo4j" />
            <span class="form-hint">默认关闭</span>
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              :icon="Search"
              @click="submit"
            >
              开始抽取
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <template v-if="result">
        <!-- 抽取结果 Raw -->
        <el-card v-if="rawList().length > 0" class="result-card" shadow="hover">
          <template #header>
            <span><el-icon><Document /></el-icon> 抽取实体</span>
          </template>
          <el-table :data="rawList()" stripe border>
            <el-table-column prop="id" label="ID" width="120" />
            <el-table-column prop="name" label="名称" min-width="200" />
            <el-table-column prop="confidence" label="置信度" width="120">
              <template #default="{ row }">
                <el-tag :type="row.confidence >= 0.8 ? 'success' : row.confidence >= 0.5 ? 'warning' : 'info'">
                  {{ (row.confidence ?? 0).toFixed(2) }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 图谱结构 Graph -->
        <el-card v-if="graph().nodes?.length || graph().relationships?.length || graph().ontology_relations?.length" class="result-card" shadow="hover">
          <template #header>
            <span><el-icon><Connection /></el-icon> 图谱结构</span>
          </template>
          <el-collapse>
            <el-collapse-item v-if="graph().nodes?.length" name="nodes">
              <template #title>
                <el-icon><List /></el-icon>
                节点 ({{ graph().nodes.length }})
              </template>
              <el-table :data="graph().nodes" stripe size="small" max-height="280">
                <el-table-column type="index" label="#" width="50" />
                <el-table-column prop="id" label="id" min-width="100" />
                <el-table-column prop="label" label="label" width="100" />
                <el-table-column prop="name" label="name" min-width="120" />
              </el-table>
            </el-collapse-item>
            <el-collapse-item v-if="graph().relationships?.length" name="rels">
              <template #title>
                关系 ({{ graph().relationships.length }})
              </template>
              <el-table :data="graph().relationships" stripe size="small" max-height="240">
                <el-table-column type="index" label="#" width="50" />
                <el-table-column prop="source" label="source" width="100" />
                <el-table-column prop="target" label="target" width="100" />
                <el-table-column prop="type" label="type" min-width="100" />
              </el-table>
            </el-collapse-item>
            <el-collapse-item v-if="graph().ontology_relations?.length" name="onto">
              <template #title>
                本体关系 ({{ graph().ontology_relations.length }})
              </template>
              <el-table :data="graph().ontology_relations" stripe size="small" max-height="240">
                <el-table-column type="index" label="#" width="50" />
                <el-table-column prop="source" label="source" width="100" />
                <el-table-column prop="target" label="target" width="100" />
                <el-table-column prop="type" label="type" min-width="100" />
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-empty
          v-else-if="rawList().length === 0 && !graph().nodes?.length && !graph().relationships?.length && !graph().ontology_relations?.length"
          description="未抽取到实体与图谱数据"
        />
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.extract-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.extract-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}

.extract-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}

.extract-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}

.extract-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.extract-inner {
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

.form-card,
.result-card {
  border-radius: var(--card-radius-lg);

  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
    display: flex;
    align-items: center;
    gap: 8px;
  }
}

.extract-form {
  max-width: 640px;
}

.form-hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--gray-500);
}

.result-card {
  margin-top: 8px;
}

@media (max-width: 768px) {
  .extract-view {
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
