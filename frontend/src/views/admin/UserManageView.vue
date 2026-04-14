<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, User, EditPen, Delete, Refresh } from '@element-plus/icons-vue'
import { listUsersApi, createUserApi, updateUserApi, deleteUserApi } from '@/api/modules/auth'

const loading = ref(false)
const rows = ref([])

const createOpen = ref(false)
const createForm = reactive({ username: '', password: '', role: 'user' })

const editOpen = ref(false)
const editForm = reactive({ id: null, username: '', role: 'user', password: '' })

const userCount = computed(() => rows.value.length)

async function load() {
  loading.value = true
  try {
    const { data } = await listUsersApi()
    rows.value = data
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '加载用户列表失败')
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  if (!createForm.username.trim() || !createForm.password) {
    ElMessage.warning('请填写用户名与密码')
    return
  }
  try {
    await createUserApi({
      username: createForm.username.trim(),
      password: createForm.password,
      role: createForm.role
    })
    ElMessage.success('已创建用户')
    createOpen.value = false
    createForm.username = ''
    createForm.password = ''
    createForm.role = 'user'
    await load()
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '创建失败')
  }
}

function openEdit(row) {
  editForm.id = row.id
  editForm.username = row.username
  editForm.role = row.role
  editForm.password = ''
  editOpen.value = true
}

async function onSaveEdit() {
  const body = { role: editForm.role }
  if (editForm.password) body.password = editForm.password
  try {
    await updateUserApi(editForm.id, body)
    ElMessage.success('已保存')
    editOpen.value = false
    await load()
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '保存失败')
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」？此操作不可恢复。`, '删除用户', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger'
    })
  } catch {
    return
  }
  try {
    await deleteUserApi(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    const msg = e.response?.data?.detail
    ElMessage.error(typeof msg === 'string' ? msg : '删除失败')
  }
}

onMounted(() => {
  load()
})
</script>

<template>
  <div class="user-manage">
    <div class="page-hero">
      <div class="hero-text">
        <p class="hero-eyebrow">系统管理</p>
        <h1 class="hero-title">用户管理</h1>
        <p class="hero-desc">创建账号、分配角色与重置密码。请谨慎删除管理员。</p>
      </div>
      <div class="hero-stat">
        <span class="stat-value">{{ userCount }}</span>
        <span class="stat-label">用户总数</span>
      </div>
    </div>

    <el-card class="table-card" shadow="never">
      <div class="card-toolbar">
        <div class="toolbar-left">
          <el-icon class="toolbar-icon" :size="20"><UserFilled /></el-icon>
          <span class="toolbar-title">用户列表</span>
        </div>
        <el-button type="primary" class="btn-new" @click="createOpen = true">
          <el-icon class="btn-icon"><Plus /></el-icon>
          新建用户
        </el-button>
      </div>

      <el-table
        v-loading="loading"
        class="user-table"
        :data="rows"
        stripe
        :empty-text="loading ? '加载中…' : '暂无用户数据'"
      >
        <el-table-column prop="id" label="用户 ID" min-width="300">
          <template #default="{ row }">
            <span class="cell-id" :title="row.id">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column prop="role" label="角色" width="120" align="center">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" effect="light" round size="small">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" min-width="180" />
        <el-table-column label="操作" width="168" fixed="right" align="right">
          <template #default="{ row }">
            <el-button class="op-btn" text type="primary" @click="openEdit(row)">
              <el-icon><EditPen /></el-icon>
              编辑
            </el-button>
            <el-button class="op-btn" text type="danger" @click="onDelete(row)">
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="createOpen"
      class="um-dialog"
      width="440px"
      align-center
      destroy-on-close
      :show-close="true"
    >
      <template #header>
        <div class="dialog-head">
          <span class="dialog-head-icon"><el-icon :size="22"><Plus /></el-icon></span>
          <span class="dialog-head-title">新建用户</span>
        </div>
      </template>
      <el-form label-position="top" class="um-form">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" placeholder="登录用户名" autocomplete="off" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input
            v-model="createForm.password"
            type="password"
            show-password
            placeholder="至少 6 位"
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createForm.role" placeholder="选择角色" style="width: 100%">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editOpen" class="um-dialog" width="440px" align-center destroy-on-close>
      <template #header>
        <div class="dialog-head">
          <span class="dialog-head-icon muted"><el-icon :size="22"><EditPen /></el-icon></span>
          <span class="dialog-head-title">编辑用户</span>
        </div>
      </template>
      <el-form label-position="top" class="um-form">
        <el-form-item label="用户名">
          <el-input v-model="editForm.username" disabled />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="新密码（留空则不修改）">
          <el-input
            v-model="editForm.password"
            type="password"
            show-password
            placeholder="可选"
            autocomplete="new-password"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">取消</el-button>
        <el-button type="primary" @click="onSaveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.user-manage {
  max-width: 1080px;
  margin: 0 auto;
  padding: 28px var(--padding-inline, 20px) 48px;
}

.page-hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
  padding: 28px 28px 26px;
  border-radius: 20px;
  background: linear-gradient(135deg, rgba(14, 165, 233, 0.12) 0%, rgba(56, 189, 248, 0.08) 100%);
  border: 1px solid rgba(14, 165, 233, 0.2);
  box-shadow: 0 4px 24px rgba(14, 165, 233, 0.08);
}

.hero-eyebrow {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.1em;
  color: var(--primary-600);
  text-transform: uppercase;
}

.hero-title {
  margin: 0 0 10px;
  font-size: 28px;
  font-weight: 700;
  color: var(--gray-900);
  letter-spacing: -0.02em;
}

.hero-desc {
  margin: 0;
  max-width: 520px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--gray-600);
}

.hero-stat {
  flex-shrink: 0;
  text-align: right;
  padding: 8px 20px;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.75);
  backdrop-filter: blur(8px);
  border: 1px solid rgba(255, 255, 255, 0.9);
}

.stat-value {
  display: block;
  font-size: 32px;
  font-weight: 700;
  color: var(--primary-600);
  line-height: 1.1;
}

.stat-label {
  font-size: 12px;
  color: var(--gray-500);
  font-weight: 500;
}

.table-card {
  border-radius: 20px;
  border: 1px solid var(--gray-200);
  overflow: hidden;
  box-shadow: var(--shadow-lg);

  :deep(.el-card__body) {
    padding: 0;
  }
}

.card-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px;
  border-bottom: 1px solid var(--gray-200);
  background: linear-gradient(180deg, #fafbfc 0%, #fff 100%);
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toolbar-icon {
  color: var(--primary-500);
}

.toolbar-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
}

.btn-new {
  border-radius: 10px;
  font-weight: 600;
  padding: 10px 18px;
  box-shadow: 0 4px 14px rgba(14, 165, 233, 0.35);
}

.btn-icon {
  margin-right: 6px;
}

.user-table {
  padding: 0 4px 12px;

  :deep(.el-table__header th) {
    background: #f8fafc !important;
    font-weight: 600;
    color: var(--gray-600);
    font-size: 13px;
  }

  :deep(.el-table__row) {
    transition: background var(--transition-fast);
  }
}

.cell-id {
  font-family: ui-monospace, 'Cascadia Code', 'SF Mono', Menlo, monospace;
  font-size: 12px;
  color: var(--gray-600);
  word-break: break-all;
}

.op-btn {
  font-weight: 500;

  .el-icon {
    margin-right: 4px;
    vertical-align: middle;
  }
}

.um-form {
  :deep(.el-form-item__label) {
    font-weight: 600;
    color: var(--gray-900);
  }

  :deep(.el-input__wrapper) {
    border-radius: 10px;
  }
}

.dialog-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dialog-head-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(14, 165, 233, 0.15), rgba(56, 189, 248, 0.12));
  color: var(--primary-600);

  &.muted {
    background: var(--gray-100);
    color: var(--gray-600);
  }
}

.dialog-head-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--gray-900);
}

@media (max-width: 640px) {
  .page-hero {
    flex-direction: column;
    align-items: flex-start;
  }

  .hero-stat {
    text-align: left;
  }
}
</style>

<style lang="scss">
/* 对话框标题由 slot 接管时隐藏默认 title 区域重复 — Element Plus 仍占一行，用深度样式压扁默认 header 若需 */
.um-dialog.el-dialog {
  border-radius: 16px;
  overflow: hidden;
}

.um-dialog .el-dialog__header {
  padding: 20px 20px 0;
  margin-right: 0;
}

.um-dialog .el-dialog__body {
  padding: 16px 20px 8px;
}

.um-dialog .el-dialog__footer {
  padding: 12px 20px 20px;
  border-top: 1px solid var(--gray-200);
  background: #fafbfc;
}
</style>
