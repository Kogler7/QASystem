<script setup>
import { nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ChatLineRound, Loading, Link, Monitor } from '@element-plus/icons-vue'
import { sendChatMessage } from '@/api'

const route = useRoute()
const router = useRouter()

const activeTab = ref('native')
const inputText = ref('')
const sending = ref(false)
const conversationId = ref('')
const chatScrollRef = ref(null)
const iframeLoading = ref(true)
const iframeError = ref(false)
const iframeSrc = ref('')
const difyWorkspaceUrl = ref('')
const messages = ref([
  {
    id: `assistant-${Date.now()}`,
    role: 'assistant',
    content: '你好，我是智能问答助手。请输入你的问题。'
  }
])

function buildHistory() {
  return messages.value
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .map((item) => ({
      role: item.role,
      content: item.content
    }))
}

function scrollToBottom() {
  nextTick(() => {
    const wrap = chatScrollRef.value?.wrapRef
    if (wrap) {
      wrap.scrollTop = wrap.scrollHeight
    }
  })
}

async function handleSend() {
  const text = inputText.value.trim()
  if (!text || sending.value) return

  const userMessage = {
    id: `user-${Date.now()}`,
    role: 'user',
    content: text
  }
  messages.value.push(userMessage)
  inputText.value = ''
  scrollToBottom()

  sending.value = true
  try {
    const history = buildHistory()
    const response = await sendChatMessage(text, history, conversationId.value)
    const data = response?.data || {}
    const assistantReply = data.reply || '暂无回复内容。'

    messages.value.push({
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: assistantReply
    })
    if (data.conversation_id) {
      conversationId.value = data.conversation_id
    }
    scrollToBottom()
  } catch (error) {
    const detail = error?.response?.data?.detail
    const message = typeof detail === 'string' ? detail : error?.message || '发送失败，请稍后重试'
    ElMessage.error(message)
    messages.value.push({
      id: `assistant-error-${Date.now()}`,
      role: 'assistant',
      content: `请求失败：${message}`
    })
    scrollToBottom()
  } finally {
    sending.value = false
  }
}

function handleKeyDown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    handleSend()
  }
}

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
  if (route.query.denied === '1') {
    ElMessage.warning('您没有权限访问该功能')
    router.replace({ query: {} })
  }
  iframeSrc.value = 'http://10.126.62.88:8007/chatbot/83KOUCt19CPrpLZ6'
  try {
    difyWorkspaceUrl.value = new URL(iframeSrc.value).origin
  } catch {
    difyWorkspaceUrl.value = ''
  }
})
</script>

<template>
  <div class="chat-page">
    <div class="page-bg-deco" aria-hidden="true">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <el-card class="chat-card" shadow="hover">
        <el-tabs v-model="activeTab" class="chat-tabs">
          <el-tab-pane name="native">
            <template #label>
              <span class="tab-label">
                <el-icon><ChatLineRound /></el-icon>
                workflow
              </span>
            </template>
          <div class="native-tab-body">
            <el-scrollbar ref="chatScrollRef" class="messages-area">
              <div class="messages-list">
                <div
                  v-for="item in messages"
                  :key="item.id"
                  class="message-row"
                  :class="item.role"
                >
                  <div class="message-bubble">
                    {{ item.content }}
                  </div>
                </div>
              </div>
            </el-scrollbar>

            <div class="composer">
              <el-input
                v-model="inputText"
                type="textarea"
                :rows="3"
                resize="none"
                placeholder="请输入你的问题，Enter 发送，Shift + Enter 换行"
                :disabled="sending"
                @keydown="handleKeyDown"
              />
              <div class="composer-actions">
                <el-button type="primary" :loading="sending" :disabled="!inputText.trim()" @click="handleSend">
                  发送
                </el-button>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane name="iframe">
            <template #label>
              <span class="tab-label">
                <el-icon><Monitor /></el-icon>
                agent
              </span>
            </template>
          <div class="iframe-tab-body">
            <div class="iframe-toolbar">
              <el-button type="primary" :disabled="!difyWorkspaceUrl" @click="openDify">
                <el-icon><Link /></el-icon>
                <span>打开 Dify 工作台</span>
              </el-button>
            </div>
            <div class="iframe-card">
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
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.chat-page {
  position: relative;
  height: calc(100vh - var(--nav-height));
  padding: 16px var(--padding-inline) 24px;
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
  overflow: hidden;
}

.page-bg-deco {
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
  width: 220px;
  height: 220px;
  top: 8%;
  right: 4%;
}

.circle-2 {
  width: 140px;
  height: 140px;
  bottom: 18%;
  left: 6%;
  animation-delay: 2s;
}

.chat-card {
  position: relative;
  z-index: 1;
  max-width: var(--content-max-width);
  margin: 0 auto;
  width: 100%;
  flex: 1;
  min-height: 0;
  border-radius: var(--card-radius-lg);
  border: 1px solid var(--gray-200);
  transition: var(--transition-normal);

  :deep(.el-card__body) {
    padding: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  &:hover {
    box-shadow: var(--shadow-hover);
  }
}

.chat-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 4px;

  /* 让 Tab 内容区占满卡片剩余高度，避免 iframe 被裁切 */
  :deep(.el-tabs) {
    height: 100%;
    display: flex;
    flex-direction: column;
  }

  :deep(.el-tabs__header) {
    margin: 0;
    padding: 12px 16px 0;
    flex-shrink: 0;
  }

  :deep(.el-tabs__content) {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  :deep(.el-tab-pane) {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  :deep(.el-tabs__nav-wrap::after) {
    display: none;
  }

  :deep(.el-tabs__active-bar) {
    background: var(--primary-500);
  }

  :deep(.el-tabs__item) {
    font-size: 15px;
    font-weight: 500;
  }

  :deep(.el-tabs__item.is-active) {
    color: var(--primary-500);
  }

  :deep(.el-tabs__item:hover) {
    color: var(--primary-500);
  }
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

:deep(.chat-tabs .el-tabs__content) {
  padding: 0 12px 12px;
}

.native-tab-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.messages-area {
  flex: 1;
  min-height: 0;
  padding: 16px 12px;
  background: linear-gradient(180deg, var(--gray-100) 0%, var(--bg-primary) 48%);
  border-radius: var(--card-radius);
}

.messages-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.message-row {
  display: flex;
}

.message-row.user {
  justify-content: flex-end;
}

.message-row.assistant {
  justify-content: flex-start;
}

.message-bubble {
  max-width: min(760px, 85%);
  padding: 12px 14px;
  border-radius: 14px;
  line-height: 1.65;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: var(--shadow-sm);
}

.message-row.user .message-bubble {
  background: var(--primary-gradient);
  color: #fff;
  border: none;
}

.message-row.assistant .message-bubble {
  background: var(--white);
  color: var(--gray-900);
  border: 1px solid var(--gray-200);
  border-left: 3px solid var(--primary-400);
}

.composer {
  border-top: 1px solid var(--gray-200);
  padding: 14px 4px 4px;
  background: var(--white);

  :deep(.el-textarea__inner) {
    border-radius: var(--card-radius);
    border-color: var(--gray-200);
    transition: border-color var(--transition-fast);

    &:focus {
      border-color: var(--primary-400);
    }
  }
}

.composer-actions {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}

.iframe-tab-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.iframe-toolbar {
  flex-shrink: 0;
  padding: 4px 8px 12px;
}

.iframe-card {
  flex: 1;
  min-height: 0;
  border-top: 1px solid var(--gray-200);
  overflow: hidden;
  position: relative;
  display: flex;
  flex-direction: column;
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
  flex: 1;
  min-height: 0;
  width: 100%;
  border: none;
}

@media (max-width: 768px) {
  .chat-page {
    padding: 12px 16px 20px;
  }
  .messages-area {
    padding: 12px 8px;
  }
  .iframe-toolbar {
    padding: 4px 4px 12px;
  }
}
</style>
