<script setup>
import { ref, onMounted, watch } from 'vue'
import { MagicStick } from '@element-plus/icons-vue'
import { nl2cypherHealthCheck, nl2cypherGenerate } from '@/api'
import { nl2cypherSplitCharacter, nl2cypherSplitRecursive, nl2cypherSplitMarkdown, nl2cypherSplitPython, nl2cypherText2vector } from '@/api'  // 新增的API导入
import { ElMessage } from 'element-plus'

const loading = ref(false)
const healthStatus = ref(null)
const healthDetail = ref('')

// 当前选中的主菜单与子菜单
const activeFeature = ref('cypher')
const activeSubFeature = ref('')

// 表单数据
const form = ref({
  question: '',
  graph_schema: `Graph schema: Relevant node labels and their properties are:
Article
Keyword
Author
Entity`,
  splitText: ''  // 新增：字符切分的文本输入
})

// 结果数据
const resultCypher = ref('')
const splitResult = ref(null)  // 新增：字符切片结果
const vectorResult = ref('')  // 文本向量化结果，显示完成信息

// nested menu definitions for sidebar
const menuItems = [
  { key: 'cypher', label: '自然语言转Cypher', tooltip: '输入自然语言, 自动生成符合图谱的 Cypher 查询' },
  {
    key: 'split',
    label: '文本切片',
    tooltip: '四种文本切片方式的入口',
    children: [
      { key: 'split-char', label: '按字符切分', tooltip: '按照字符分割文本，适合简单分段' },
      { key: 'split-recursive', label: '递归切分', tooltip: '递归地按字符分片，可生成更小的段落' },
      { key: 'split-markdown', label: 'Markdown 切分', tooltip: '基于 Markdown 语法进行智能切片' },
      { key: 'split-python', label: 'Python 切分', tooltip: '对 Python 源码进行语言感知的切片' },
    ],
  },
  { key: 'vector', label: '文本向量化与存储', tooltip: '将文本向量化并写入数据库以备后续检索' },
]

// helper map for tooltip lookup
const tooltips = {}
menuItems.forEach(item => {
  tooltips[item.key] = item.tooltip
  if (item.children) {
    item.children.forEach(c => {
      tooltips[c.key] = c.tooltip
    })
  }
})

// whenever feature changes we clear any in-progress data
// watch main feature changes; clear state and reset sub-feature if needed
watch(activeFeature, (newVal) => {
  form.value.splitText = ''
  splitResult.value = null
  vectorResult.value = null
  resultCypher.value = ''
  if (newVal !== 'split') {
    activeSubFeature.value = ''
  } else {
    // select default child if none chosen
    if (!activeSubFeature.value) {
      activeSubFeature.value = menuItems.find(i => i.key === 'split')?.children?.[0]?.key || ''
    }
  }
})

const checkHealth = async () => {
  healthStatus.value = null
  healthDetail.value = ''
  try {
    const { data } = await nl2cypherHealthCheck()
    if (data?.status === 'ok') {
      healthStatus.value = 'ok'
    } else {
      healthStatus.value = 'error'
      healthDetail.value = data?.detail || '模块未就绪'
    }
  } catch {
    healthStatus.value = 'error'
    healthDetail.value = '请检查后端是否启动并安装 algorithm/NL_to_cypher 依赖，配置 Ollama'
  }
}

const submit = async () => {
  const active = activeFeature.value === 'split' ? activeSubFeature.value : activeFeature.value
  if (active === 'cypher') {
    await submitCypher()
  } else if (active === 'split-char') {
    await submitSplitCharacter()
  } else if (active === 'split-recursive') {
    await submitSplitRecursive()
  } else if (active === 'split-markdown') {
    await submitSplitMarkdown()
  } else if (active === 'split-python') {
    await submitSplitPython()
  } else if (active === 'vector') {
    await submitVector()
  }
}

const submitSplitRecursive = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitRecursive(text)
    splitResult.value = data
    ElMessage.success('递归字符切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '递归字符切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitSplitMarkdown = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitMarkdown(text)
    splitResult.value = data
    ElMessage.success('Markdown 切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Markdown 切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitSplitPython = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitPython(text)
    splitResult.value = data
    ElMessage.success('Python 切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Python 切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitCypher = async () => {
  const question = form.value.question?.trim()
  if (!question) {
    ElMessage.warning('请输入自然语言问题')
    return
  }
  loading.value = true
  resultCypher.value = ''
  try {
    const { data } = await nl2cypherGenerate({
      question,
      graph_schema: form.value.graph_schema?.trim() ?? ''
    })
    resultCypher.value = data?.cypher ?? ''
    ElMessage.success('Cypher 生成完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || 'Cypher 生成失败')
    resultCypher.value = ''
  } finally {
    loading.value = false
  }
}

const submitSplitCharacter = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要切分的文本')
    return
  }
  loading.value = true
  splitResult.value = null
  try {
    const { data } = await nl2cypherSplitCharacter(text)
    splitResult.value = data
    ElMessage.success('字符切分完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '字符切分失败')
    splitResult.value = null
  } finally {
    loading.value = false
  }
}

const submitVector = async () => {
  const text = form.value.splitText?.trim()
  if (!text) {
    ElMessage.warning('请输入要向量化的文本')
    return
  }
  loading.value = true
  vectorResult.value = null
  try {
    const { data } = await nl2cypherText2vector(text)
    vectorResult.value = '存储完成'
    ElMessage.success('向量化存储完成')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '向量化失败')
    vectorResult.value = null
  } finally {
    loading.value = false
  }
}

// 复制结果到剪贴板
const copyToClipboard = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch (err) {
    console.error('复制失败:', err)
    ElMessage.error('复制失败')
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="nl2cypher-view">
    <div class="nl2cypher-bg-deco">
      <span class="circle circle-1"></span>
      <span class="circle circle-2"></span>
    </div>

    <!-- 顶部标题与健康状态固定区域 -->
    <div class="nl2cypher-header">
      <div class="header-left"></div>
      <div class="header-right">
        <h1 class="page-title">
          <span class="title-highlight">文本向量化与切片组件库</span>
        </h1>
        <p class="page-desc">
          将文本转为向量存储、不同逻辑下的文本切片、自然语言转Cypher。
        </p>
        <div v-if="healthStatus !== null" class="health-row">
          <span class="health-label">服务状态：</span>
          <el-tag v-if="healthStatus === 'ok'" type="success" size="small">正常</el-tag>
          <el-tooltip v-else :content="healthDetail" placement="bottom" :show-after="300">
            <el-tag type="danger" size="small">不可用</el-tag>
          </el-tooltip>
          <el-button link type="primary" size="small" @click="checkHealth">重新检测</el-button>
        </div>
                <!-- 新增组件功能说明卡片 -->
        <el-card class="info-card" shadow="hover">
          <template #header>
            <span>组件功能说明</span>
          </template>
          <div class="info-content">
            <div class="section">
              <h4>功能概述</h4>
              <p>提供以下核心能力：</p>
              <ul>
                <li>文本按照多策略切片</li>
                <li>对切片结果生成向量并存储于数据库</li>
                <li>自然语言问题转换为 Cypher 查询</li>
              </ul>
            </div>
            <div class="section">
              <h4>使用流程</h4>
              <ol>
                <li>从左侧菜单选择所需功能（切片 / 向量化 / Cypher）</li>
                <li>填写输入文本或问题</li>
                <li>点击提交并查看结果</li>
                <li>向量化操作会在后台写入数据库，可用于后续检索</li>
              </ol>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <!-- 主区域：左侧菜单 右侧动态内容 -->
    <div class="nl2cypher-inner">
      <!-- 侧边菜单（自定义项，无路由） -->
      <div class="nl2cypher-menu">
        <template v-for="item in menuItems" :key="item.key">
          <div
            class="menu-entry"
            :class="{ active: activeFeature === item.key }"
            @click="activeFeature = item.key"
          >
            <el-tooltip :content="tooltips[item.key]" placement="right">
              <span>{{ item.label }}</span>
            </el-tooltip>
          </div>

          <!-- 子菜单显示在文本切片下面 -->
          <div
            v-if="item.children && activeFeature === item.key"
            class="submenu"
          >
            <div
              v-for="child in item.children"
              :key="child.key"
              class="menu-entry sub-entry"
              :class="{ active: activeSubFeature === child.key }"
              @click.stop="activeSubFeature = child.key"
            >
              <el-tooltip :content="tooltips[child.key]" placement="right">
                <span>{{ child.label }}</span>
              </el-tooltip>
            </div>
          </div>
        </template>
      </div>

      <div class="nl2cypher-content">
        <!-- 生成 Cypher 内容区 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'cypher'">
          <template #header>
            <span>生成 Cypher</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="自然语言问题" required>
              <el-input
                v-model="form.question"
                type="textarea"
                :rows="3"
                placeholder="例如：找出图中所有 Article 节点的总数。"
              />
            </el-form-item>
            <el-form-item label="图谱 Schema">
              <el-input
                v-model="form.graph_schema"
                type="textarea"
                :rows="6"
                placeholder="节点标签与属性描述，用于约束生成的 Cypher。"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                生成 Cypher
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="resultCypher" class="result-box">
            <div class="result-header">
              <p class="result-label">生成的 Cypher：</p>
              <el-button type="primary" size="small" @click="copyToClipboard(resultCypher)" text>
                复制
              </el-button>
            </div>
            <pre class="result-cypher">{{ resultCypher }}</pre>
          </div>
        </el-card>

        <!-- 字符切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-char'">
          <template #header>
            <span>字符切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入文本" required>
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容..."
                resize="vertical"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <el-button 
                type="primary" 
                size="small" 
                @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                text
              >
                复制JSON
              </el-button>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- 递归字符切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-recursive'">
          <template #header>
            <span>递归字符切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入文本" required>
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的文本内容..."
                resize="vertical"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <el-button 
                type="primary" 
                size="small" 
                @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                text
              >
                复制JSON
              </el-button>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- Markdown 切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-markdown'">
          <template #header>
            <span>Markdown 切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入文本" required>
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的 Markdown 内容..."
                resize="vertical"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <el-button 
                type="primary" 
                size="small" 
                @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                text
              >
                复制JSON
              </el-button>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- Python 切分功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'split' && activeSubFeature === 'split-python'">
          <template #header>
            <span>Python 切分</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入文本" required>
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要切分的 Python 源码..."
                resize="vertical"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始切分
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="splitResult" class="result-box">
            <div class="result-header">
              <p class="result-label">切分结果：</p>
              <el-button 
                type="primary" 
                size="small" 
                @click="copyToClipboard(JSON.stringify(splitResult, null, 2))" 
                text
              >
                复制JSON
              </el-button>
            </div>
            
            <div v-if="splitResult.chunks && splitResult.chunks.length > 0" class="split-result">
              <div class="result-summary">
                <el-tag type="info" size="small">共 {{ splitResult.chunks.length }} 个片段</el-tag>
              </div>
              
              <div class="chunks-list">
                <div v-for="(chunk, index) in splitResult.chunks" :key="index" class="chunk-item">
                  <div class="chunk-header">
                    <span class="chunk-index">片段 {{ index + 1 }}</span>
                    <span class="chunk-length">{{ chunk.length || chunk.text?.length || 0 }} 字符</span>
                    <el-button 
                      size="small" 
                      @click="copyToClipboard(chunk.text || chunk)" 
                      type="text"
                    >
                      复制片段
                    </el-button>
                  </div>
                  <pre class="chunk-content">{{ chunk.text || chunk }}</pre>
                </div>
              </div>
            </div>
            
            <div v-else class="json-result">
              <pre class="result-json">{{ JSON.stringify(splitResult, null, 2) }}</pre>
            </div>
          </div>
        </el-card>

        <!-- 文本向量化功能 -->
        <el-card class="form-card" shadow="hover" v-if="activeFeature === 'vector'">
          <template #header>
            <span>向量化并存储</span>
          </template>
          <el-form label-width="100px" class="nl2cypher-form">
            <el-form-item label="输入文本" required>
              <el-input
                v-model="form.splitText"
                type="textarea"
                :rows="8"
                placeholder="请输入要向量化的文本内容..."
                resize="vertical"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="loading"
                :icon="MagicStick"
                @click="submit"
              >
                开始向量化
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="vectorResult" class="result-box">
            <div class="result-header">
              <p class="result-label">{{ vectorResult }}</p>
            </div>
          </div>
        </el-card>

        <!-- 其他功能保持占位或说明 -->
        <el-card class="form-card" shadow="hover" v-if="
          activeFeature === 'split' && !activeSubFeature ||
          (activeFeature !== 'cypher' && activeFeature !== 'split' && activeFeature !== 'vector')
        ">
          <template #header>
            <span v-if="activeFeature === 'split'">
              文本切片
            </span>
            <span v-else-if="activeFeature === 'vector'">
              文本向量化与存储
            </span>
            <span v-else>
              占位功能
            </span>
          </template>
          <div class="placeholder-box">
            <p v-if="activeFeature === 'split'">
              请选择左侧子菜单中的一种切片方式。
            </p>
            <p v-else>
              该界面尚未实现具体逻辑，仅作为占位。
            </p>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.nl2cypher-view {
  min-height: calc(100vh - var(--nav-height));
  padding: 40px var(--padding-inline) 80px;
  position: relative;
  overflow: hidden;
}

.nl2cypher-bg-deco {
  pointer-events: none;
  position: absolute;
  inset: 0;
}
.nl2cypher-bg-deco .circle {
  position: absolute;
  border-radius: 50%;
  background: var(--primary-gradient);
  opacity: 0.06;
  animation: float 5s ease-in-out infinite;
}
.nl2cypher-bg-deco .circle-1 {
  width: 200px;
  height: 200px;
  top: 15%;
  right: 8%;
}
.nl2cypher-bg-deco .circle-2 {
  width: 140px;
  height: 140px;
  bottom: 25%;
  left: 8%;
  animation-delay: 2s;
}

@keyframes float {
  0%, 100% { transform: translateY(0) scale(1); }
  50% { transform: translateY(-20px) scale(1.05); }
}

.nl2cypher-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  max-width: var(--content-max-width);
  margin: 0 auto 24px;
  padding: 0 0 24px;
  border-bottom: 1px solid var(--gray-200);
}
.nl2cypher-header .header-right {
  text-align: left;
  flex: 1;
}

/* 新增信息卡片样式 */
.info-card {
  margin: 16px 16px 16px 16px; /* include horizontal gap from edges, less bottom margin */
  border: 1px solid #007bff; /* explicit blue border */
  padding: 8px 8px 8px 8px;
  border-radius: 6px;
}
/* ensure header text is blue and more specific */
.info-card .el-card__header,
.info-card :deep(.el-card__header) {
  color: #44aff6 !important;
  font-weight: 600;
  font-size: 18px; /* larger header */
  margin-top: -10px;
  margin-bottom: -26px;
}
.info-content {
  font-size: 14px;
  margin-top: 0px;
  margin-bottom: -20px;
  color: var(--gray-700);
}
.info-content .section {
  margin-bottom: 10px;
}
.info-content h4 {
  margin: 0 0 4px;
  font-size: 15px;
  color: var(--gray-900);
  font-weight: 600;
}
.info-content ul,
.info-content ol {
  margin: 4px 0 0 20px;
}
.info-content li {
  margin: 2px 0;
}

.nl2cypher-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  position: relative;
  z-index: 1;
  display: flex;
  gap: 24px;
}
.nl2cypher-menu {
  width: 160px;
  flex-shrink: 0;
}

.menu-entry {
  padding: 8px 12px;
  border-radius: 4px;
  cursor: pointer;
  transition: background-color 0.2s, color 0.2s;
  margin-bottom: 4px;
  font-size: 14px;
  color: var(--gray-700);
}
.menu-entry:hover {
  background-color: var(--gray-100);
  color: var(--primary);
}
.menu-entry.active {
  background-color: var(--primary-light);
  color: #258bde;
  font-weight: 500;
}
.nl2cypher-content {
  flex: 1;
  min-width: 0;
}

.page-title {
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 2px; /* reduced spacing */
  color: var(--gray-900);
}
.page-desc {
  font-size: 15px;
  color: var(--gray-600);
  margin-bottom: 4px; /* smaller gap under description */
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
  margin-bottom: 24px;
  :deep(.el-card__header) {
    font-weight: 600;
    color: var(--gray-900);
  }
}

.nl2cypher-form {
  max-width: 720px;
}

.result-box {
  margin-top: 20px;
  padding: 20px;
  background: var(--gray-50);
  border-radius: 8px;
  border: 1px solid var(--gray-200);
}
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  .result-label {
    font-weight: 600;
    margin: 0;
    color: var(--gray-800);
    font-size: 15px;
  }
}
.result-cypher, .result-json {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  overflow: auto;
  max-height: 400px;
  padding: 12px;
  background: var(--gray-100);
  border-radius: 6px;
  border: 1px solid var(--gray-300);
}
.result-cypher {
  color: var(--primary-dark);
}
.result-json {
  color: var(--gray-700);
}

// 切片结果样式
.split-result {
  .result-summary {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--gray-200);
  }
  .chunks-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .chunk-item {
    background: white;
    border-radius: 6px;
    border: 1px solid var(--gray-200);
    overflow: hidden;
  }
  .chunk-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: var(--gray-50);
    border-bottom: 1px solid var(--gray-200);
  }
  .chunk-index {
    font-weight: 500;
    color: var(--gray-800);
    font-size: 13px;
  }
  .chunk-length {
    color: var(--gray-600);
    font-size: 12px;
  }
  .chunk-content {
    margin: 0;
    padding: 12px;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow: auto;
    background: var(--gray-50);
  }
}

@media (max-width: 768px) {
  .nl2cypher-view {
    padding: 24px 20px 60px;
  }
  .page-title {
    font-size: 22px;
  }
  .page-desc {
    font-size: 14px;
  }
  .nl2cypher-inner {
    flex-direction: column;
  }
  .nl2cypher-menu {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
    .menu-entry {
      flex: 1;
      min-width: 120px;
      text-align: center;
      margin-bottom: 0;
    }
  }
  .chunk-header {
    flex-wrap: wrap;
    gap: 8px;
  }
}

/* submenu styling */
.submenu {
  margin-left: 16px;
  margin-top: 4px;
}
.sub-entry {
  padding-left: 24px;
  font-size: 13px;
}
.sub-entry.active {
  font-weight: 500;
}
</style>