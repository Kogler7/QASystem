<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Document, FolderOpened, Search, Upload } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const activeTab = ref('index')

const tabs = [
  { name: 'index', label: '首页', icon: FolderOpened },
  { name: 'upload', label: '知识沉淀', icon: Upload },
  { name: 'list', label: '文档列表', icon: Document },
  { name: 'query', label: '知识检索', icon: Search }
]

watch(
  () => route.path,
  (path) => {
    const match = path.match(/\/knowledge\/(\w+)/)
    if (match) activeTab.value = match[1]
  },
  { immediate: true }
)

const onTabClick = (pane) => {
  const name = pane?.paneName?.value ?? pane?.paneName ?? pane
  if (name) router.replace(`/knowledge/${name}`)
}
</script>

<template>
  <div class="knowledge-layout">
    <div class="layout-inner">
      <el-tabs v-model="activeTab" class="knowledge-tabs" @tab-click="onTabClick">
        <el-tab-pane
          v-for="tab in tabs"
          :key="tab.name"
          :name="tab.name"
        >
          <template #label>
            <span class="tab-label">
              <el-icon><component :is="tab.icon" /></el-icon>
              {{ tab.label }}
            </span>
          </template>
        </el-tab-pane>
      </el-tabs>
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </div>
  </div>
</template>

<style scoped lang="scss">
.knowledge-layout {
  min-height: calc(100vh - var(--nav-height));
  padding: 24px var(--padding-inline) 60px;
}

.layout-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
}

.knowledge-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 24px;
  }

  :deep(.el-tabs__item) {
    font-size: 15px;
    font-weight: 500;
  }

  :deep(.el-tabs__nav-wrap::after) {
    display: none;
  }

  :deep(.el-tabs__active-bar) {
    background: var(--primary-500);
  }

  :deep(.el-tabs__item.is-active) {
    color: var(--primary-500);
  }

  :deep(.el-tabs__ink-bar) {
    background: var(--primary-500);
  }

  :deep(.el-tabs__item:hover) {
    color: var(--primary-500);
  }

  :deep(.el-tabs__content) {
    overflow: visible;
  }
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-normal);
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
