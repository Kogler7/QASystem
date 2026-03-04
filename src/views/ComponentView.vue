<script setup>
import { Aim, Connection, RefreshRight, DataAnalysis } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

const router = useRouter()

const components = [
    {
    id: 'data-preprocessing',
    title: '多源数据预处理组件',
    desc: '从多个数据源获取数据，进行清洗、转换和标准化处理，将数据转换为统一格式，确保数据质量和一致性。',
    icon: DataAnalysis,
    color: 'var(--warning)'
  },
  {
    id: 'intent-recognition',
    title: '意图识别组件',
    desc: '对用户输入进行意图分类，识别查询、故障诊断、规范查询等类型，并路由到对应处理流程。',
    icon: Aim,
    color: 'var(--primary-500)'
  },
  {
    id: 'question-splitting',
    title: '问题切分组件',
    desc: '将复杂问题拆解为多个子问题，支持多轮追问与子问题并行检索，提升回答准确度。',
    icon: Connection,
    color: 'var(--success)'
  },
  {
    id: 'knowledge-graph-update',
    title: '知识图谱更新组件',
    desc: '对接知识库与图谱数据源，支持增量更新与实体关系维护，保障检索与推理的时效性。',
    icon: RefreshRight,
    color: 'var(--info)'
  }
]

// 组件点击处理函数
const handleComponentClick = (componentId) => {
  console.log(`点击了组件: ${componentId}`)
  // 预留跳转逻辑
  // 未来可以根据componentId跳转到对应的组件详情页面
  // 例如: router.push(`/component/${componentId}`)
  
  // 这里可以添加具体的组件处理逻辑
  switch (componentId) {
    case 'data-preprocessing':
      // 多源数据预处理模块处理逻辑
      break
    case 'intent-recognition':
      // 意图识别组件处理逻辑
      break
    case 'question-splitting':
      // 问题切分组件处理逻辑
      break
    case 'knowledge-graph-update':
      // 知识图谱更新组件处理逻辑
      break
    default:
      break
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
        系统核心组件说明与功能展示，包括多源数据预处理、意图识别、问题切分与知识图谱更新等模块。
      </p>

      <div class="component-grid">
        <el-card
          v-for="item in components"
          :key="item.id"
          class="component-card"
          shadow="hover"
          @click="handleComponentClick(item.id)"
          style="cursor: pointer;"
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
