<script setup>
import { ref, onMounted } from 'vue'
import { Upload, Delete, Document, Loading } from '@element-plus/icons-vue'
import { getDocuments, uploadDocument, deleteDocument } from '@/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { preprocAcceptAttr, preprocFormatHint, isPreprocExtension } from '@/constants/preprocFormats'

const fileList = ref([])
const loading = ref(false)
const uploadLoading = ref(false)

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

const handleUpload = async (options) => {
  const { file } = options
  if (!isPreprocExtension(file?.name)) {
    ElMessage.error(preprocFormatHint)
    return
  }
  const formData = new FormData()
  formData.append('file', file)
  uploadLoading.value = true
  try {
    await uploadDocument(formData)
    ElMessage.success('上传成功')
    fetchList()
  } catch (err) {
    ElMessage.error(err?.message || '上传失败')
  } finally {
    uploadLoading.value = false
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

onMounted(() => {
  fetchList()
})
</script>

<template>
  <div class="document-manage">
    <el-card class="upload-card" shadow="hover">
      <template #header>
        <span>上传文档</span>
      </template>
      <el-upload
        drag
        :auto-upload="true"
        :show-file-list="false"
        :http-request="handleUpload"
        :disabled="uploadLoading"
        :accept="preprocAcceptAttr"
      >
        <el-icon class="upload-icon"><Upload /></el-icon>
        <div class="upload-text">将文件拖到此处，或<em>点击上传</em></div>
        <template #tip>
          <div class="upload-tip">{{ preprocFormatHint }}</div>
        </template>
      </el-upload>
      <div v-if="uploadLoading" class="upload-loading">
        <el-icon class="is-loading"><Loading /></el-icon>
        上传中...
      </div>
    </el-card>

    <el-card class="list-card" shadow="hover">
      <template #header>
        <span>文档列表</span>
      </template>
      <el-table
        v-loading="loading"
        :data="fileList"
        stripe
        style="width: 100%"
      >
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
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="danger" link :icon="Delete" @click="handleRemove(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && fileList.length === 0" description="暂无文档" />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.document-manage {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.upload-card,
.list-card {
  border-radius: var(--card-radius);
}

.upload-icon {
  font-size: 48px;
  color: var(--primary-400);
}

.upload-text {
  margin-top: 8px;
  font-size: 14px;
  color: var(--gray-600);

  em {
    color: var(--primary-500);
    font-style: normal;
  }
}

.upload-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gray-500);
}

.upload-loading {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--gray-600);
}

.doc-icon {
  margin-right: 8px;
  vertical-align: middle;
  color: var(--gray-500);
}
</style>
