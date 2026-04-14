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
  <div class="user-manage-layout">
    <div class="home-bg-deco" aria-hidden="true">
      <span class="circle circle-1" />
      <span class="circle circle-2" />
    </div>

    <div class="layout-inner">
      <h1 class="page-title">
        <span class="title-highlight">用户管理</span>
      </h1>
      <p class="page-desc">
        管理系统账号与角色：创建用户、分配管理员或普通用户、重置密码。删除操作不可恢复，请谨慎处理管理员账号。
      </p>
      <p class="page-meta">当前共 <strong>{{ userCount }}</strong> 个用户</p>

      <el-card class="list-card" shadow="hover">
        <template #header>
          <div class="header-row">
            <span class="header-title">用户列表</span>
            <div class="header-actions">
              <el-button link type="primary" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
              <el-button type="primary" :icon="Plus" @click="createOpen = true">新建用户</el-button>
            </div>
          </div>
        </template>

        <el-table
          v-loading="loading"
          :data="rows"
          stripe
          class="user-table"
          :empty-text="loading ? '加载中…' : '暂无用户'"
        >
          <el-table-column type="index" label="#" width="56" />
          <el-table-column prop="id" label="用户 ID" min-width="280">
            <template #default="{ row }">
              <span class="cell-id" :title="row.id">{{ row.id }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="username" label="用户名" min-width="140">
            <template #default="{ row }">
              <el-icon class="name-icon"><User /></el-icon>
              {{ row.username }}
            </template>
          </el-table-column>
          <el-table-column prop="role" label="角色" width="120" align="center">
            <template #default="{ row }">
              <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" effect="light" size="small">
                {{ row.role === 'admin' ? '管理员' : '普通用户' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" min-width="180" />
          <el-table-column label="操作" width="168" fixed="right">
            <template #default="{ row }">
              <el-space class="action-space" :size="10" alignment="center" :wrap="false">
                <el-button type="primary" link :icon="EditPen" @click="openEdit(row)">编辑</el-button>
                <el-button type="danger" link :icon="Delete" @click="onDelete(row)">删除</el-button>
              </el-space>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && rows.length === 0" description="暂无用户" />
      </el-card>
    </div>

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
/* 与知识库 KnowledgeLayout / KnowledgeIndex 一致的外层与装饰 */
.user-manage-layout {
  min-height: calc(100vh - var(--nav-height));
  padding: 24px var(--padding-inline) 60px;
  position: relative;
  overflow: hidden;
}

.home-bg-deco {
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
  width: 240px;
  height: 240px;
  top: 8%;
  right: 6%;
}

.circle-2 {
  width: 160px;
  height: 160px;
  bottom: 18%;
  left: 4%;
  animation-delay: 2s;
}

.layout-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
}

.page-title {
  font-size: 32px;
  font-weight: 700;
  margin-bottom: 12px;
  color: var(--gray-900);
}

.page-desc {
  font-size: 16px;
  color: var(--gray-600);
  margin-bottom: 12px;
  max-width: 1000px;
  line-height: 1.6;
}

.page-meta {
  font-size: 14px;
  color: var(--gray-500);
  margin-bottom: 28px;

  strong {
    color: var(--primary-600);
    font-weight: 700;
  }
}

/* 与 DocumentList list-card 一致 */
.list-card {
  border-radius: var(--card-radius);
  transition: var(--transition-normal);

  &:hover {
    box-shadow: var(--shadow-hover);
  }
}

.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.user-table {
  width: 100%;

  :deep(.el-table__header th) {
    font-weight: 600;
    color: var(--gray-600);
    font-size: 13px;
  }
}

.name-icon {
  margin-right: 8px;
  vertical-align: middle;
  color: var(--gray-500);
}

.cell-id {
  font-family: ui-monospace, 'Cascadia Code', 'SF Mono', Menlo, monospace;
  font-size: 12px;
  color: var(--gray-600);
  word-break: break-all;
}

.action-space {
  flex-wrap: nowrap;
}

.um-form {
  :deep(.el-form-item__label) {
    font-weight: 600;
    color: var(--gray-900);
  }

  :deep(.el-input__wrapper) {
    border-radius: var(--button-radius);
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
  border-radius: var(--card-radius);
  background: var(--gray-100);
  color: var(--primary-600);

  &.muted {
    color: var(--gray-600);
  }
}

.dialog-head-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--gray-900);
}

@media (max-width: 768px) {
  .user-manage-layout {
    padding: 24px 20px 60px;
  }

  .page-title {
    font-size: 24px;
  }

  .page-desc {
    font-size: 14px;
  }
}
</style>

<style lang="scss">
.um-dialog.el-dialog {
  border-radius: var(--card-radius-lg);
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
  background: var(--gray-100);
}
</style>
