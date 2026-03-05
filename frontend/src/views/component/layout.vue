<script setup>
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()

/** 仅在子功能页显示返回按钮，主页面（列表）不显示 */
const showBack = () => {
  const p = route.path
  return p.startsWith('/component/') && p !== '/component/index'
}
const goBack = () => router.push('/component')
</script>

<template>
  <div class="component-layout">
    <div v-if="showBack()" class="layout-back">
      <div class="layout-back-inner">
        <el-button text type="primary" :icon="ArrowLeft" @click="goBack">
          返回组件管理
        </el-button>
      </div>
    </div>
    <router-view v-slot="{ Component }">
      <transition name="fade" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>
</template>

<style scoped lang="scss">
.component-layout {
  min-height: calc(100vh - var(--nav-height));
  position: relative;
}

.layout-back {
  position: sticky;
  top: var(--nav-height);
  z-index: 100;
  background: var(--bg-primary);
  border-bottom: 1px solid var(--gray-200);
  margin-bottom: 0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.layout-back-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 12px var(--padding-inline);
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
