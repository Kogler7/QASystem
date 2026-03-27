<script setup>
import { nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading, Link } from '@element-plus/icons-vue'
import { sendChatMessage } from '@/api'

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
    <header class="chat-header">
      <div class="chat-title">
        智能问答
      </div>
      <div class="chat-status">
        <span v-if="activeTab === 'native'">{{ sending ? '回答中...' : '在线' }}</span>
        <span v-else>iframe 模式</span>
      </div>
    </header>
    <div class="chat-card">
      <el-tabs v-model="activeTab" class="chat-tabs">
        <el-tab-pane label="workflow" name="native">
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

        <el-tab-pane label="chatflow(iframe)" name="iframe">
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

.chat-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--gray-900);
}

.chat-status {
  color: var(--gray-600);
  font-size: 14px;
}

.chat-card {
  flex: 1;
  min-height: 0;
  background: var(--white);
  border-radius: var(--card-radius-lg);
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--gray-200);
  display: flex;
}

.chat-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

:deep(.chat-tabs .el-tabs__content) {
  flex: 1;
  min-height: 0;
}

:deep(.chat-tabs .el-tab-pane) {
  height: 100%;
}

.native-tab-body {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.messages-area {
  flex: 1;
  min-height: 0;
  padding: 16px;
  background: var(--bg-primary);
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
  padding: 10px 12px;
  border-radius: 10px;
  line-height: 1.6;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}

.message-row.user .message-bubble {
  background: var(--el-color-primary);
  color: #fff;
}

.message-row.assistant .message-bubble {
  background: #fff;
  color: var(--gray-900);
  border: 1px solid var(--gray-200);
}

.composer {
  border-top: 1px solid var(--gray-200);
  padding: 12px;
  background: #fff;
}

.composer-actions {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}

.iframe-tab-body {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.iframe-toolbar {
  padding: 0 16px 12px;
}

.iframe-card {
  flex: 1;
  min-height: 0;
  border-top: 1px solid var(--gray-200);
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
  min-height: 700px;
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
  .messages-area {
    padding: 12px;
  }
  .iframe-toolbar {
    padding: 0 12px 12px;
  }
}
</style>
