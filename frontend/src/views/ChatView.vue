<script setup>
import { ref, onMounted } from 'vue'
import { Loading } from '@element-plus/icons-vue'

const iframeLoading = ref(true)
const iframeError = ref(false)
const iframeSrc = ref('')

const handleIframeLoad = () => {
  iframeLoading.value = false
  iframeError.value = false
}

const handleIframeError = () => {
  iframeLoading.value = false
  iframeError.value = true
  console.error('Iframe加载失败:', iframeSrc.value)
}

onMounted(() => {
  // 使用相对路径，由用户访问网站时的同一域名/端口请求，需在 Nginx 等将 /chatbot 反向代理到 Dify
iframeSrc.value = 'http://10.126.62.88:8007/chatbot/SbVdbOB0Y7vL3PyF'
  iframeLoading.value = true
  iframeError.value = false
})
</script>

<template>
  <div class="chat-wrapper">
    <div class="iframe-container">
      <div v-if="iframeLoading && !iframeError" class="iframe-loading">
        <el-icon class="is-loading" :size="32"><Loading /></el-icon>
        <span>加载中...</span>
      </div>
      <div v-else-if="iframeError" class="iframe-error">
        <span>聊天界面加载失败，请检查网络或稍后重试</span>
      </div>
      <iframe
        v-if="iframeSrc"
        :src="iframeSrc"
        class="chatbot-iframe"
        frameborder="0"
        allow="microphone"
        @load="handleIframeLoad"
        @error="handleIframeError"
      />
    </div>
  </div>
</template>

<style scoped lang="scss">
.chat-wrapper {
  width: 100%;
  min-height: calc(100vh - var(--nav-height));
  position: relative;
  background: var(--gray-50);
}

.iframe-container {
  width: 100%;
  height: calc(100vh - var(--nav-height));
  position: relative;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 24px;
  box-sizing: border-box;
}

.iframe-loading,
.iframe-error {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--gray-500);
  font-size: 14px;
  z-index: 2;
}

.chatbot-iframe {
  width: 90%;
  height: 100%;
  min-height: 700px;
  border: none;
}

@media (max-width: 768px) {
  .iframe-container {
    padding: 16px;
  }

  .chatbot-iframe {
    width: 100%;
  }
}
</style>
