<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { ChatDotRound, Delete, Loading, Promotion } from '@element-plus/icons-vue'
import { sendChatMessage } from '../api/chat'
import { renderMarkdown } from '../utils/markdown'
import { ElMessage } from 'element-plus'

const messages = ref([])
const inputText = ref('')
const loading = ref(false)
const chatContainer = ref(null)
// 控制显示模式：true 显示 iframe，false 显示原有聊天界面
const useIframe = ref(false)
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

// 切换模式时重置 iframe 状态
const toggleIframe = () => {
  useIframe.value = !useIframe.value
  if (useIframe.value) {
    // 切换到 iframe 模式时重置状态
    iframeLoading.value = true
    iframeError.value = false
  }
}

// 延迟加载 iframe，避免阻塞初始渲染
onMounted(() => {
  // 确保组件已挂载后再设置 iframe src
  iframeSrc.value = 'http://localhost/chatbot/78WZ21FuxzRdqaF2'
  // 重置 iframe 状态，避免从其他页面返回时状态异常
  iframeLoading.value = true
  iframeError.value = false
})

const scrollToBottom = () => {
  nextTick(() => {
    const el = chatContainer.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

const send = async () => {
  const text = inputText.value?.trim()
  if (!text || loading.value) return

  const userMsg = {
    role: 'user',
    content: text,
    time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  messages.value.push(userMsg)
  inputText.value = ''
  scrollToBottom()

  loading.value = true
  const assistantPlaceholder = {
    role: 'assistant',
    content: '',
    time: '',
    loading: true
  }
  messages.value.push(assistantPlaceholder)
  scrollToBottom()

  try {
    const history = messages.value
      .filter((m) => m.role && !m.loading)
      .slice(-10)
      .map((m) => ({ role: m.role, content: m.content }))
    const { data } = await sendChatMessage(text, history)
    const rawReply = data?.success === false
      ? (data?.error ?? data?.reply ?? '请求失败')
      : (data?.reply ?? data?.answer ?? data?.content ?? '')
    const reply = (typeof rawReply === 'string' && rawReply.trim()) ? rawReply.trim() : '暂无回复内容。'
    assistantPlaceholder.content = reply
    assistantPlaceholder.time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    assistantPlaceholder.loading = false
  } catch (err) {
    // 检查是否为超时错误
    const isTimeout = err?.code === 'ECONNABORTED' || err?.message?.includes('timeout') || err?.message?.includes('超时')
    
    let displayMsg = '网络或服务异常，请稍后重试。'
    
    if (isTimeout) {
      displayMsg = '请求超时，AI响应时间较长，请稍后重试或检查网络连接。'
    } else {
      const res = err?.response?.data
      const detail = res?.detail || res?.error || res?.reply || err?.message
      const tip = typeof detail === 'string' ? detail : (Array.isArray(detail) ? detail[0]?.msg : null)
      if (tip && tip.trim()) {
        displayMsg = tip.trim()
      }
    }
    
    assistantPlaceholder.content = displayMsg
    assistantPlaceholder.time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    assistantPlaceholder.loading = false
    ElMessage.error(displayMsg)
  } finally {
    loading.value = false
    scrollToBottom()
  }
}

const clearChat = () => {
  messages.value = []
  ElMessage.success('已清空对话')
}

const handleKeydown = (e) => {
  if (e.key === 'Enter' && e.ctrlKey) {
    e.preventDefault()
    send()
  }
}
</script>

<template>
  <div class="chat-wrapper">
    <!-- 使用 iframe 显示外部聊天机器人 -->
    <div v-if="useIframe" class="iframe-container">
      <iframe
        v-if="iframeSrc"
        :src="iframeSrc"
        class="chatbot-iframe"
        frameborder="0"
        allow="microphone"
        @load="handleIframeLoad"
        @error="handleIframeError">
      </iframe>
    </div>

    <!-- 原有的聊天界面 -->
    <div v-else class="chat-view">
    <div class="chat-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
      <span class="circle circle-3"></span>
    </div>

    <div class="chat-container">
      <el-card class="chat-card" shadow="hover">
        <template #header>
          <div class="chat-header">
            <div class="header-left">
              <el-icon :size="24" color="var(--primary-500)">
                <ChatDotRound />
              </el-icon>
              <span class="chat-title">智能问答助手</span>
            </div>
            <el-button type="danger" plain size="small" :icon="Delete" @click="clearChat">
              清空对话
            </el-button>
          </div>
        </template>

        <div ref="chatContainer" class="message-area">
          <template v-if="messages.length === 0">
            <div class="empty-tip">
              <el-icon :size="48" color="var(--gray-200)">
                <Promotion />
              </el-icon>
              <p>请输入您的问题开始咨询</p>
              <p class="tip-hint">例如：地铁列车制动系统故障如何处理？</p>
            </div>
          </template>
          <template v-else>
            <div
              v-for="(msg, index) in messages"
              :key="index"
              :class="['message-row', msg.role]"
            >
              <el-avatar
                :size="40"
                :style="{
                  background: msg.role === 'user' ? 'var(--primary-gradient)' : 'var(--gray-200)',
                  color: msg.role === 'user' ? '#fff' : 'var(--gray-600)'
                }"
              >
                <el-icon v-if="msg.role === 'user'"><Promotion /></el-icon>
                <el-icon v-else><ChatDotRound /></el-icon>
              </el-avatar>
              <div class="message-body">
                <div v-if="msg.loading" class="loading-wrap">
                  <el-icon class="is-loading" :size="20"><Loading /></el-icon>
                  <span>AI正在思考中...</span>
                </div>
                <div v-else class="bubble markdown-body" v-html="renderMarkdown(msg.content)"></div>
                <span v-if="msg.time && !msg.loading" class="message-time">{{ msg.time }}</span>
              </div>
            </div>
          </template>
        </div>

        <div class="input-area">
          <el-input
            v-model="inputText"
            type="textarea"
            :rows="3"
            placeholder="请输入您的问题，例如：地铁列车制动系统故障如何处理？"
            resize="none"
            @keydown="handleKeydown"
          />
          <div class="input-footer">
            <span class="hint">按 Ctrl + Enter 快速发送</span>
            <el-button
              type="primary"
              :loading="loading"
              :icon="Promotion"
              @click="send"
            >
              发送
            </el-button>
          </div>
        </div>
      </el-card>
    </div>
    </div>
    
    <!-- 切换按钮 -->
    <el-button
      class="switch-mode-btn"
      type="primary"
      circle
      :icon="useIframe ? ChatDotRound : Promotion"
      @click="toggleIframe"
      :title="useIframe ? '切换到内置聊天' : '切换到外部聊天'"
    />
  </div>
</template>

<style scoped lang="scss">
.chat-wrapper {
  width: 100%;
  min-height: calc(100vh - var(--nav-height));
  position: relative;
  background: var(--gray-50);
}

.chat-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 24px;
  position: relative;
  overflow: hidden;
}

.chat-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
  overflow: hidden;
}

.circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.08;
  animation: float 4s ease-in-out infinite;
}

.circle-1 {
  width: 200px;
  height: 200px;
  top: 10%;
  left: 5%;
  animation-delay: 0s;
}

.circle-2 {
  width: 150px;
  height: 150px;
  top: 60%;
  right: 10%;
  animation-delay: 1s;
}

.circle-3 {
  width: 100px;
  height: 100px;
  bottom: 15%;
  left: 20%;
  animation-delay: 2s;
}

.chat-container {
  max-width: 1200px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}

.chat-card {
  border-radius: var(--card-radius-lg);

  :deep(.el-card__header) {
    padding: 16px 24px;
    border-bottom: 1px solid var(--gray-200);
  }

  :deep(.el-card__body) {
    padding: 0;
    display: flex;
    flex-direction: column;
  }
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.chat-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--gray-900);
}

.message-area {
  min-height: 360px;
  max-height: 60vh;
  overflow-y: auto;
  padding: 24px;
}

.empty-tip {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 320px;
  color: var(--gray-500);
  font-size: 14px;

  p {
    margin: 8px 0 0;
  }

  .tip-hint {
    font-size: 13px;
    color: var(--gray-200);
  }
}

.message-row {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  align-items: flex-start;

  &.user {
    flex-direction: row-reverse;

    .message-body {
      align-items: flex-end;
    }

    .bubble {
      background: var(--primary-gradient);
      color: white;
      border-radius: 12px 12px 4px 12px;
    }
  }
}

.message-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  max-width: 75%;
}

.bubble {
  padding: 12px 16px;
  background: var(--gray-100);
  border-radius: 12px 12px 12px 4px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--gray-900);
}

.bubble.markdown-body :deep(pre) {
  margin: 8px 0;
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13px;
}

.bubble.markdown-body :deep(code) {
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
}

.bubble.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.bubble.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

.loading-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  color: var(--gray-500);
  font-size: 14px;
}

.message-time {
  font-size: 12px;
  color: var(--gray-500);
  margin-top: 4px;
}

.input-area {
  padding: 16px 24px 24px;
  border-top: 1px solid var(--gray-200);
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}

.hint {
  font-size: 12px;
  color: var(--gray-500);
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

.chatbot-iframe {
  width: 90%;
  height: 100%;
  min-height: 650px;
  border: none;
}

.switch-mode-btn {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 999; /* 低于导航栏的 z-index: 1000 */
  width: 56px;
  height: 56px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  transition: all 0.3s ease;
  pointer-events: auto; /* 确保可以点击 */

  &:hover {
    transform: scale(1.1) translateY(-2px);
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.2);
  }

  &:active {
    transform: scale(0.95);
  }
}

@media (max-width: 768px) {
  .message-body {
    max-width: 85%;
  }

  .switch-mode-btn {
    bottom: 16px;
    right: 16px;
    width: 48px;
    height: 48px;
  }
}
</style>
