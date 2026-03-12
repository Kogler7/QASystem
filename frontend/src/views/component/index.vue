<script setup>
import { useRouter } from 'vue-router'
import { componentMenuItems } from './config'

const router = useRouter()

const handleComponentClick = (item) => {
  if (item.route) {
    router.push({ name: item.route })
  }
}
</script>

<template>
  <div class="map-view">
    <div class="map-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="map-inner">
      <h1 class="page-title">
        <span class="title-highlight">组件管理</span>
      </h1>
      <p class="page-desc">
        系统核心组件说明与功能展示，包括意图识别、知识图谱更新、知识抽取、答案生成等模块。
      </p>

      <div class="component-grid">
        <el-card
          v-for="item in componentMenuItems"
          :key="item.id"
          class="component-card"
          shadow="hover"
          @click="handleComponentClick(item)"
          :style="{ cursor: item.route ? 'pointer' : 'default' }"
        >
          <div class="card-icon-wrap" :style="{ color: item.color }">
            <el-icon :size="36"><component :is="item.icon" /></el-icon>
          </div>
          <h3 class="card-title">{{ item.title }}</h3>
          <p class="card-desc">{{ item.desc }}</p>
          <p v-if="!item.route" class="card-tip">敬请期待</p>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.map-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.map-bg-deco {
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

.map-inner {
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

.component-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--grid-gap);
}

.component-card {
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

.card-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gray-500);
}

@media (max-width: 1024px) {
  .component-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .map-view {
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
