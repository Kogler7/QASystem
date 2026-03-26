<script setup>
import { useRouter } from 'vue-router'
import { Document, Upload, Search } from '@element-plus/icons-vue'

const router = useRouter()

const shortcuts = [
  {
    title: '知识沉淀',
    desc: '上传文档，完成预处理、切分、抽取并更新知识图谱',
    icon: Upload,
    path: '/knowledge/upload',
    color: 'var(--primary-500)'
  },
  {
    title: '文档列表',
    desc: '查看、管理已上传文档',
    icon: Document,
    path: '/knowledge/list',
    color: 'var(--primary-500)'
  },
  {
    title: '知识检索',
    desc: '面向知识库的全文与语义检索',
    icon: Search,
    path: '/knowledge/query',
    color: 'var(--success)'
  }
]
</script>

<template>
  <div class="knowledge-home">
    <div class="home-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="home-inner">
      <h1 class="page-title">
        <span class="title-highlight">知识库</span>
      </h1>
      <p class="page-desc">
        本知识库为轨道交通行业知识服务系统提供文档与知识支撑，支持文档上传、管理与多维度检索。
      </p>

      <div class="shortcut-grid">
        <el-card
          v-for="item in shortcuts"
          :key="item.title"
          class="shortcut-card"
          shadow="hover"
          @click="router.push(item.path)"
          style="cursor: pointer"
        >
          <div class="card-icon-wrap" :style="{ color: item.color }">
            <el-icon :size="36"><component :is="item.icon" /></el-icon>
          </div>
          <h3 class="card-title">{{ item.title }}</h3>
          <p class="card-desc">{{ item.desc }}</p>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.knowledge-home {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.home-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}

.circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}

.circle-1 {
  width: 240px;
  height: 240px;
  top: 10%;
  right: 5%;
}

.circle-2 {
  width: 160px;
  height: 160px;
  bottom: 20%;
  left: 5%;
  animation-delay: 2s;
}

.home-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
}

.page-title {
  font-size: 32px;
  font-weight: 700;
  margin-bottom: 12px;
  color: var(--gray-900);
}

.page-desc {
  font-size: 16px;
  color: var(--gray-600);
  margin-bottom: 40px;
  max-width: 1000px;
}

.shortcut-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--grid-gap);
}

.shortcut-card {
  border-radius: var(--card-radius-lg);
  transition: var(--transition-normal);

  :deep(.el-card__body) {
    padding: var(--padding-inline);
  }

  &:hover {
    transform: translateY(-4px);
    box-shadow: var(--shadow-hover);
  }
}

.card-icon-wrap {
  width: 64px;
  height: 64px;
  border-radius: 16px;
  background: var(--gray-100);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 20px;
}

.card-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 10px;
}

.card-desc {
  font-size: 14px;
  color: var(--gray-600);
  line-height: 1.6;
}

@media (max-width: 1024px) {
  .shortcut-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .knowledge-home {
    padding: 24px 20px 60px;
  }
  .page-title {
    font-size: 24px;
  }
  .page-desc {
    font-size: 14px;
  }
}
</style>
