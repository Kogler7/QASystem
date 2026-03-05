<script setup>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Menu, Close, ChatDotRound } from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const menuVisible = ref(false)

const navItems = [
  { path: '/', name: '首页' },
  { path: '/chat', name: '智能问答' },
  { path: '/component', name: '组件管理' },
  { path: '/knowledge', name: '知识库' }
]

const isActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

const goConsult = () => {
  menuVisible.value = false
  router.push('/chat')
}

const goTo = (path) => {
  menuVisible.value = false
  router.push(path)
}

const isMobile = computed(() => {
  if (typeof window === 'undefined') return false
  return window.innerWidth < 768
})
</script>

<template>
  <div class="app-wrap">
    <header class="app-header">
      <div class="header-inner">
        <router-link to="/" class="logo-area" @click="menuVisible = false">
          <div class="logo-icon">
            <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect width="40" height="40" rx="10" fill="url(#logoGrad)" />
              <path d="M12 20h16M12 14h10M12 26h14" stroke="white" stroke-width="2" stroke-linecap="round" />
              <defs>
                <linearGradient id="logoGrad" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
                  <stop stop-color="#0EA5E9" />
                  <stop offset="1" stop-color="#38BDF8" />
                </linearGradient>
              </defs>
            </svg>
          </div>
          <div class="logo-text">
            <span class="logo-zh">轨道交通知识服务系统</span>
            <span class="logo-en">Rail Transit Knowledge Service</span>
          </div>
        </router-link>

        <nav class="nav-menu" :class="{ open: menuVisible }">
          <a
            v-for="item in navItems"
            :key="item.path"
            :href="item.path"
            :class="['nav-item', { active: isActive(item.path) }]"
            @click.prevent="goTo(item.path)"
          >
            {{ item.name }}
          </a>
        </nav>

        <div class="header-actions">
          <el-button type="primary" class="action-button primary" @click="goConsult">
            <el-icon><ChatDotRound /></el-icon>
            <span>开始提问</span>
          </el-button>
          <button type="button" class="hamburger" aria-label="菜单" @click="menuVisible = !menuVisible">
            <el-icon v-if="!menuVisible"><Menu /></el-icon>
            <el-icon v-else><Close /></el-icon>
          </button>
        </div>
      </div>
    </header>

    <main class="app-main">
      <router-view v-slot="{ Component, route: currentRoute }">
        <transition name="fade" mode="out-in">
          <component 
            :is="Component" 
            :key="currentRoute.path"
          />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<style scoped lang="scss">
.app-wrap {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-header {
  position: sticky;
  top: 0;
  z-index: 1000;
  height: var(--nav-height);
  background: rgba(255, 255, 255, 0.8);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-bottom: 1px solid rgba(226, 232, 240, 0.8);
  box-shadow: var(--shadow-lg);
}

.header-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  height: 100%;
  padding: 0 var(--padding-inline);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}

.logo-area {
  display: flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  color: var(--gray-900);
}

.logo-icon {
  width: 40px;
  height: 40px;
  flex-shrink: 0;
}

.logo-icon svg {
  width: 100%;
  height: 100%;
  display: block;
}

.logo-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.logo-zh {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
}

.logo-en {
  font-size: 11px;
  color: var(--gray-500);
}

.nav-menu {
  display: flex;
  align-items: center;
  gap: 8px;
}

.nav-item {
  padding: 8px 16px;
  border-radius: var(--button-radius);
  color: var(--gray-600);
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  transition: var(--transition-fast);
}

.nav-item:hover {
  color: var(--primary-500);
  background: rgba(14, 165, 233, 0.08);
}

.nav-item.active {
  color: var(--primary-500);
  background: rgba(14, 165, 233, 0.12);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hamburger {
  display: none;
  padding: 8px;
  border: none;
  background: none;
  cursor: pointer;
  color: var(--gray-600);
  font-size: 20px;
}

.app-main {
  flex: 1;
  position: relative;
  overflow: visible; /* 确保内容可以正常显示 */
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-normal);
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (max-width: 1024px) {
  .nav-menu {
    gap: 4px;
  }
  .nav-item {
    padding: 8px 12px;
    font-size: 13px;
  }
}

@media (max-width: 768px) {
  .nav-menu {
    position: fixed;
    top: var(--nav-height);
    left: 0;
    right: 0;
    flex-direction: column;
    background: var(--bg-card);
    backdrop-filter: blur(20px);
    padding: 16px;
    gap: 4px;
    box-shadow: var(--shadow-lg);
    border-bottom: 1px solid var(--gray-200);
    transform: translateY(-100%);
    opacity: 0;
    visibility: hidden;
    transition: var(--transition-normal);
  }

  .nav-menu.open {
    transform: translateY(0);
    opacity: 1;
    visibility: visible;
  }

  .nav-item {
    width: 100%;
    padding: 12px 16px;
    text-align: left;
  }

  .hamburger {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .logo-en {
    display: none;
  }
}
</style>
