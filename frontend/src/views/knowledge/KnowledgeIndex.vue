<script setup>
import { useRouter } from 'vue-router'
import { Document, Upload, Search } from '@element-plus/icons-vue'

const router = useRouter()

const shortcuts = [
  {
    title: '文档管理',
    desc: '上传、管理知识库文档',
    icon: Upload,
    path: '/knowledge/documents',
    color: 'var(--primary-500)'
  },
  {
    title: '知识检索',
    desc: '全文与语义检索',
    icon: Search,
    path: '/knowledge/query',
    color: 'var(--success)'
  }
]
</script>

<template>
  <div class="knowledge-index">
    <el-card class="overview-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <el-icon :size="22" color="var(--primary-500)"><Document /></el-icon>
          <span>知识库概览</span>
        </div>
      </template>
      <p class="overview-desc">
        本知识库为轨道交通行业知识服务系统提供文档与知识支撑，支持文档上传、管理与多维度检索。
      </p>
      <div class="shortcut-grid">
        <el-card
          v-for="item in shortcuts"
          :key="item.title"
          class="shortcut-card"
          shadow="hover"
          @click="router.push(item.path)"
        >
          <div class="shortcut-icon" :style="{ color: item.color }">
            <el-icon :size="28"><component :is="item.icon" /></el-icon>
          </div>
          <h4 class="shortcut-title">{{ item.title }}</h4>
          <p class="shortcut-desc">{{ item.desc }}</p>
        </el-card>
      </div>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.knowledge-index {
  padding: 0;
}

.overview-card {
  border-radius: var(--card-radius-lg);

  :deep(.el-card__header) {
    padding: 18px 24px;
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-900);
  }

  :deep(.el-card__body) {
    padding: 24px;
  }
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.overview-desc {
  font-size: 14px;
  color: var(--gray-600);
  line-height: 1.6;
  margin-bottom: 24px;
}

.shortcut-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 20px;
}

.shortcut-card {
  border-radius: var(--card-radius);
  cursor: pointer;
  transition: var(--transition-normal);

  :deep(.el-card__body) {
    padding: 20px;
  }

  &:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-md);
  }
}

.shortcut-icon {
  margin-bottom: 12px;
}

.shortcut-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 6px;
}

.shortcut-desc {
  font-size: 13px;
  color: var(--gray-500);
  line-height: 1.5;
}

@media (max-width: 768px) {
  .shortcut-grid {
    grid-template-columns: 1fr;
  }
}
</style>
