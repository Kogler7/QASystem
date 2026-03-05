<script setup>
import { ref, onMounted } from 'vue'
import { Upload, Document } from '@element-plus/icons-vue'
import { documentPreprocHealthCheck, documentPreprocConvert } from '@/api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')
const fileList = ref([])
const result = ref(null)

const acceptTypes = '.pdf,.docx,.doc,.xlsx,.xls,.html,.htm'

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await documentPreprocHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '文档预处理模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端并安装 algorithm/preproc 依赖'
  }
}

const handleChange = (_uploadFile, uploadFiles) => {
  fileList.value = uploadFiles?.length ? [uploadFiles[0].raw] : []
}

const submit = async () => {
  if (!fileList.value?.length) {
    ElMessage.warning('请先选择文件')
    return
  }
  const file = fileList.value[0]
  loading.value = true
  result.value = null
  try {
    const { data } = await documentPreprocConvert(file)
    result.value = data
    if (data?.success) {
      ElMessage.success('转换完成')
    } else {
      ElMessage.warning(data?.error || '转换未成功')
    }
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '转换失败')
    result.value = null
  } finally {
    loading.value = false
  }
}

const contentDisplay = () => {
  if (!result.value) return ''
  const c = result.value.content_preview ?? result.value.content
  if (c == null) return ''
  return typeof c === 'string' ? c : JSON.stringify(c)
}

onMounted(checkHealth)
</script>

<template>
  <div class="preproc-view">
    <div class="preproc-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <div class="preproc-inner">
      <h1 class="page-title">
        <span class="title-highlight">文档预处理</span>
      </h1>
      <p class="page-desc">
        上传 PDF、DOCX、Excel、HTML 等文档，转换为 Markdown/文本，便于后续检索与知识抽取。
      </p>

      <div v-if="healthStatus !== null" class="health-row">
        <span class="health-label">服务状态：</span>
        <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
        <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
          <el-tag type="danger" size="small">不可用</el-tag>
        </el-tooltip>
        <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
      </div>

      <el-card class="form-card" shadow="hover">
        <template #header>
          <span>上传并转换</span>
        </template>
        <el-upload
          :auto-upload="false"
          :limit="1"
          :on-change="handleChange"
          :on-exceed="() => ElMessage.warning('仅支持单文件')"
          :accept="acceptTypes"
          drag
        >
          <el-icon class="el-icon--upload"><Upload /></el-icon>
          <div class="el-upload__text">将文件拖到此处，或<em>点击选择</em></div>
          <template #tip>
            <div class="el-upload__tip">支持 PDF、DOCX、DOC、XLSX、XLS、HTML</div>
          </template>
        </el-upload>
        <el-button
          type="primary"
          :loading="loading"
          :icon="Document"
          style="margin-top: 16px"
          @click="submit"
        >
          开始转换
        </el-button>

        <div v-if="result" class="result-box">
          <p class="result-label">转换结果</p>
          <p v-if="result.success" class="result-meta">
            输出路径：<code>{{ result.output_path || '—' }}</code>
            <span v-if="result.file_type"> · 类型 {{ result.file_type }}</span>
          </p>
          <p v-if="result.error" class="result-error">{{ result.error }}</p>
          <div v-if="contentDisplay()" class="result-content">
            <pre>{{ contentDisplay() }}</pre>
          </div>
          <el-collapse v-if="result.metadata">
            <el-collapse-item title="查看元数据" name="meta">
              <pre class="result-json">{{ JSON.stringify(result.metadata, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.preproc-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.preproc-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.preproc-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.preproc-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.preproc-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

.preproc-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.page-title {
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 4px;
  color: var(--gray-900);
}
.page-desc {
  font-size: 15px;
  color: var(--gray-600);
  margin-bottom: 8px;
}

.health-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.health-label {
  font-size: 14px;
  color: var(--gray-600);
}

.form-card {
  border-radius: var(--card-radius-lg);
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.result-box {
  margin-top: 20px;
  padding: 16px;
  background: var(--gray-100);
  border-radius: 8px;
}
.result-label {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--gray-800);
}
.result-meta {
  font-size: 13px;
  color: var(--gray-600);
  margin: 4px 0;
}
.result-meta code {
  font-size: 12px;
  background: var(--gray-200);
  padding: 2px 6px;
  border-radius: 4px;
}
.result-error {
  font-size: 14px;
  color: var(--el-color-danger);
  margin: 8px 0;
}
.result-content {
  margin-top: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  overflow: auto;
  max-height: 360px;
  background: #fff;
}
.result-content pre {
  margin: 0;
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}
.result-json {
  font-size: 12px;
  overflow: auto;
  max-height: 240px;
  margin: 0;
  white-space: pre-wrap;
}

@media (max-width: 768px) {
  .preproc-view {
    padding: 24px 20px 60px;
  }
  .page-title {
    font-size: 22px;
  }
  .page-desc {
    font-size: 14px;
  }
}
</style>
