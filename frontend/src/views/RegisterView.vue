<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, Key } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import AuthPageShell from '@/components/AuthPageShell.vue'

const router = useRouter()
const auth = useAuthStore()

const username = ref('')
const password = ref('')
const password2 = ref('')
const loading = ref(false)

async function onSubmit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  if (password.value.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  if (password.value !== password2.value) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    await auth.register(username.value.trim(), password.value)
    ElMessage.success('注册成功，请登录')
    await router.replace('/login')
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthPageShell
    eyebrow="创建账户"
    title="注册"
    subtitle="新账号默认为普通用户；若系统尚无用户，首个注册账号将成为管理员。"
  >
    <el-form class="auth-form" label-position="top" size="large" @submit.prevent="onSubmit">
      <el-form-item label="用户名">
        <el-input
          v-model="username"
          class="auth-input"
          placeholder="设置登录用户名"
          autocomplete="username"
          :prefix-icon="User"
        />
      </el-form-item>
      <el-form-item label="密码">
        <el-input
          v-model="password"
          class="auth-input"
          type="password"
          placeholder="至少 6 位"
          autocomplete="new-password"
          show-password
          :prefix-icon="Lock"
        />
      </el-form-item>
      <el-form-item label="确认密码">
        <el-input
          v-model="password2"
          class="auth-input"
          type="password"
          placeholder="再次输入密码"
          autocomplete="new-password"
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
        {{ loading ? '提交中…' : '创建账号' }}
      </el-button>
    </el-form>

    <template #footer>
      <span class="footer-muted">已有账号？</span>
      <el-button class="header-auth-btn" plain @click="router.push('/login')">
        <el-icon><Key /></el-icon>
        去登录
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
    margin-bottom: 18px;
  }
}

.auth-input {
  :deep(.el-input__wrapper) {
    border-radius: 12px;
    padding-left: 12px;
    box-shadow: 0 0 0 1px var(--gray-200) inset;
    transition: box-shadow var(--transition-fast);
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
  box-shadow: 0 8px 20px rgba(14, 165, 233, 0.35);
}

.footer-muted {
  color: var(--gray-500);
}
</style>
