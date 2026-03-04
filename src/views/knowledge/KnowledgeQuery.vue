<script setup>
import { ref } from 'vue'
import { Search, Loading } from '@element-plus/icons-vue'
import { knowledgeQuery as queryApi } from '../../api/knowledge'
import { ElMessage } from 'element-plus'

const queryText = ref('')
const loading = ref(false)
const results = ref([])

const search = async () => {
  const q = queryText.value?.trim()
  if (!q) {
    ElMessage.warning('请输入检索关键词')
    return
  }
  loading.value = true
  results.value = []
  try {
    const { data } = await queryApi(q)
    const list = data?.results ?? data?.list ?? data?.data ?? (Array.isArray(data) ? data : [])
    results.value = list
    if (list.length === 0) ElMessage.info('未检索到相关结果')
  } catch (err) {
    ElMessage.error(err?.message || '检索失败')
    results.value = []
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="knowledge-query">
    <el-card class="search-card" shadow="hover">
      <div class="search-box">
        <el-input
          v-model="queryText"
          placeholder="输入关键词进行知识库检索"
          size="large"
          clearable
          @keyup.enter="search"
        >
          <template #append>
            <el-button :loading="loading" :icon="Search" @click="search">
              检索
            </el-button>
          </template>
        </el-input>
      </div>
    </el-card>

    <el-card v-if="results.length > 0" class="results-card" shadow="hover">
      <template #header>
        <span>检索结果</span>
      </template>
      <div v-loading="loading" class="results-list">
        <div
          v-for="(item, index) in results"
          :key="index"
          class="result-item"
        >
          <div class="result-title">
            {{ item.title ?? item.name ?? item.content?.slice(0, 50) ?? '无标题' }}
          </div>
          <div class="result-content">
            {{ item.content ?? item.text ?? item.snippet ?? '-' }}
          </div>
          <div v-if="item.source" class="result-meta">
            {{ item.source }}
          </div>
        </div>
      </div>
    </el-card>

    <el-empty
      v-else-if="!loading && queryText && results.length === 0 && !loading"
      description="暂无检索结果"
    />
  </div>
</template>

<style scoped lang="scss">
.knowledge-query {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.search-card {
  border-radius: var(--card-radius);
}

.search-box {
  :deep(.el-input-group__append) {
    padding: 0;
    .el-button {
      margin: 0;
      border-radius: 0 var(--button-radius) var(--button-radius) 0;
    }
  }
}

.results-card {
  border-radius: var(--card-radius);
}

.results-list {
  min-height: 120px;
}

.result-item {
  padding: 16px 0;
  border-bottom: 1px solid var(--gray-200);

  &:last-child {
    border-bottom: none;
  }
}

.result-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 8px;
}

.result-content {
  font-size: 14px;
  color: var(--gray-600);
  line-height: 1.6;
}

.result-meta {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gray-500);
}
</style>
