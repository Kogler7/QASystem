<script setup>
import { ref, onMounted } from 'vue'
import { Delete, Document, Download } from '@element-plus/icons-vue'
import { getDocuments, deleteDocument, downloadDocument } from '@/api'
import { ElMessage, ElMessageBox } from 'element-plus'

const fileList = ref([])
const loading = ref(false)

const fetchList = async () => {
  loading.value = true
  try {
    const { data } = await getDocuments()
    fileList.value = Array.isArray(data) ? data : data?.list ?? data?.items ?? []
  } catch (err) {
    fileList.value = []
    ElMessage.warning('获取文档列表失败，请检查后端服务')
  } finally {
    loading.value = false
  }
}

const handleRemove = async (row) => {
  try {
    await ElMessageBox.confirm('确定删除该文档？', '提示', {
      type: 'warning'
    })
    await deleteDocument(row.id ?? row.docId ?? row._id)
    ElMessage.success('已删除')
    fetchList()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  }
}

const handleDownload = async (row) => {
  const id = row.id ?? row.docId ?? row._id
  const filename = row.name ?? row.fileName ?? row.title ?? 'document'
  if (!id) {
    ElMessage.error('缺少文档ID，无法下载')
    return
  }
  try {
    const resp = await downloadDocument(id)
    const blob = resp?.data instanceof Blob ? resp.data : new Blob([resp?.data ?? ''])
    const url = URL.createObjectURL(blob)
    const a = window.document.createElement('a')
    a.href = url
    a.download = filename
    window.document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error('下载失败')
  }
}

onMounted(fetchList)
</script>

<template>
  <div class="document-list">
    <el-card class="list-card" shadow="hover">
      <template #header>
        <div class="header-row">
          <span>文档列表</span>
          <el-button link type="primary" @click="fetchList">刷新</el-button>
        </div>
      </template>
      <el-table v-loading="loading" :data="fileList" stripe style="width: 100%">
        <el-table-column type="index" label="#" width="56" />
        <el-table-column prop="name" label="文件名" min-width="200">
          <template #default="{ row }">
            <el-icon class="doc-icon"><Document /></el-icon>
            {{ row.name ?? row.fileName ?? row.title ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="size" label="大小" width="120">
          <template #default="{ row }">
            {{ row.size ? `${(row.size / 1024).toFixed(1)} KB` : '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="createTime" label="上传时间" width="180">
          <template #default="{ row }">
            {{ row.createTime ?? row.uploadTime ?? row.createdAt ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-space class="action-space" :size="10" alignment="center" :wrap="false">
              <el-button type="primary" link :icon="Download" @click="handleDownload(row)">下载</el-button>
              <el-button type="danger" link :icon="Delete" @click="handleRemove(row)">删除</el-button>
            </el-space>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && fileList.length === 0" description="暂无文档" />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.list-card {
  border-radius: var(--card-radius);
}

.doc-icon {
  margin-right: 8px;
  vertical-align: middle;
  color: var(--gray-500);
}

.action-space {
  display: inline-flex;
  white-space: nowrap;
}
</style>
