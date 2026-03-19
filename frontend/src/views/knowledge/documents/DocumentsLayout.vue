<script setup>
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Upload, Document } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()

const active = ref('list')
const tabs = [
  { name: 'list', label: '文档列表', icon: Document, path: '/knowledge/documents/list' },
  { name: 'upload', label: '上传文档', icon: Upload, path: '/knowledge/documents/upload' }
]

watch(
  () => route.path,
  (path) => {
    if (path.includes('/knowledge/documents/upload')) active.value = 'upload'
    else active.value = 'list'
  },
  { immediate: true }
)

const onTabClick = (pane) => {
  const name = pane?.paneName?.value ?? pane?.paneName ?? pane
  const tab = tabs.find((t) => t.name === name)
  if (tab) router.replace(tab.path)
}
</script>

<template>
  <div class="documents-layout">
    <el-tabs v-model="active" class="documents-tabs" @tab-click="onTabClick">
      <el-tab-pane v-for="tab in tabs" :key="tab.name" :name="tab.name">
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
</template>

<style scoped lang="scss">
.documents-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.documents-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 12px;
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
