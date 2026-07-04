<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, Plus } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import AuthPageShell from '@/components/AuthPageShell.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const username = ref('')
const password = ref('')
const loading = ref(false)

async function onSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    await router.replace(redirect && redirect.startsWith('/') ? redirect : '/chat')
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthPageShell
    eyebrow="欢迎回来"
    title="登录账户"
    subtitle="登录后可使用智能问答；管理员还可使用知识库、组件管理与用户管理。"
  >
    <el-form class="auth-form" label-position="top" size="large" @submit.prevent="onSubmit">
      <el-form-item label="用户名">
        <el-input
          v-model="username"
          class="auth-input"
          placeholder="请输入用户名"
          autocomplete="username"
          :prefix-icon="User"
        />
      </el-form-item>
      <el-form-item label="密码">
        <el-input
          v-model="password"
          class="auth-input"
          type="password"
          placeholder="请输入密码"
          autocomplete="current-password"
          show-password
          :prefix-icon="Lock"
          @keyup.enter="onSubmit"
        />
      </el-form-item>
      <el-button
        type="primary"
        class="auth-submit"
        native-type="submit"
        :loading="loading"
        @click="onSubmit"
      >
        {{ loading ? '登录中…' : '登录' }}
      </el-button>
    </el-form>

    <template #footer>
      <span class="footer-muted">还没有账号？</span>
      <el-button class="header-auth-btn" plain @click="router.push('/register')">
        <el-icon><Plus /></el-icon>
        注册
      </el-button>
    </template>
  </AuthPageShell>
</template>

<style scoped lang="scss">
.auth-form {
  :deep(.el-form-item__label) {
    font-weight: 600;
    font-size: 13px;
    color: var(--gray-900);
    margin-bottom: 8px;
  }

  :deep(.el-form-item) {
    margin-bottom: 20px;
  }
}

.auth-input {
  :deep(.el-input__wrapper) {
    border-radius: 12px;
    padding-left: 12px;
    box-shadow: 0 0 0 1px var(--gray-200) inset;
    transition: box-shadow var(--transition-fast), background var(--transition-fast);
  }

  :deep(.el-input__wrapper:hover) {
    box-shadow: 0 0 0 1px #cbd5e1 inset;
  }

  :deep(.el-input__wrapper.is-focus) {
    box-shadow: 0 0 0 2px rgba(14, 165, 233, 0.35) inset;
  }
}

.auth-submit {
  width: 100%;
  height: 48px;
  margin-top: 8px;
  border-radius: 12px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
  box-shadow: 0 8px 20px rgba(14, 165, 233, 0.35);
}

.footer-muted {
  color: var(--gray-500);
}
</style>
