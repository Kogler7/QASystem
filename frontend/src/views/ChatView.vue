<script setup>
import { ref, onMounted } from 'vue'
import { Loading, Link } from '@element-plus/icons-vue'

const iframeLoading = ref(true)
const iframeError = ref(false)
const iframeSrc = ref('')
const difyWorkspaceUrl = ref('')

const onLoad = () => {
  iframeLoading.value = false
  iframeError.value = false
}

const onError = () => {
  iframeLoading.value = false
  iframeError.value = true
}

const openDify = () => difyWorkspaceUrl.value && window.open(difyWorkspaceUrl.value, '_blank', 'noopener,noreferrer')

onMounted(() => {
  iframeSrc.value = 'http://10.126.62.88:8007/chatbot/SbVdbOB0Y7vL3PyF'
  try {
    difyWorkspaceUrl.value = new URL(iframeSrc.value).origin
  } catch {
    difyWorkspaceUrl.value = ''
  }
})
</script>

<template>
  <div class="chat-page">
    <header class="chat-header">
      <el-button type="primary" :disabled="!difyWorkspaceUrl" @click="openDify">
        <el-icon><Link /></el-icon>
        <span>打开 Dify 工作台</span>
      </el-button>
    </header>
    <div class="chat-card">
      <div v-if="iframeLoading && !iframeError" class="state-overlay">
        <el-icon class="is-loading" :size="40"><Loading /></el-icon>
        <span>加载中...</span>
      </div>
      <div v-else-if="iframeError" class="state-overlay">
        <span>加载失败，请检查网络或稍后重试</span>
      </div>
      <iframe
        v-if="iframeSrc"
        :src="iframeSrc"
        class="chat-iframe"
        frameborder="0"
        allow="microphone"
        @load="onLoad"
        @error="onError"
      />
    </div>
  </div>
</template>

<style scoped lang="scss">
.chat-page {
  height: calc(100vh - var(--nav-height));
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 16px var(--padding-inline) 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--bg-primary);
}

.chat-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.chat-card {
  flex: 1;
  min-height: 0;
  background: var(--white);
  border-radius: var(--card-radius-lg);
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--gray-200);
  overflow: hidden;
  position: relative;
}

.state-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: var(--bg-primary);
  color: var(--gray-600);
  font-size: 14px;
  z-index: 2;
}

.chat-iframe {
  display: block;
  width: 100%;
  height: 100%;
  border: none;
}

@media (max-width: 768px) {
  .chat-page {
    padding: 12px 16px 0;
  }
  .chat-header {
    flex-direction: column;
    align-items: stretch;
  }
  .chat-title {
    font-size: 18px;
  }
}
</style>
