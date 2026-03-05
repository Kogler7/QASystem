<script setup>
import { useRouter } from 'vue-router'
import {
  ChatDotRound,
  Document,
  MapLocation,
  Collection,
  ArrowRight
} from '@element-plus/icons-vue'

const router = useRouter()

const goConsult = () => router.push('/chat')
const goKnowledge = () => router.push('/knowledge')
const goTo = (path) => router.push(path)

const quickQuestions = [
  '地铁列车制动系统故障如何处理？',
  '查询城市轨道交通设计规范',
  'CBTC系统工作原理是什么？',
  '轨道交通信号系统有哪些类型？'
]

const stats = [
  { value: '7×24', label: '服务时长（小时）' },
  { value: '1000+', label: '知识覆盖（条）' },
  { value: '500+', label: '文档数量' },
  { value: '< 2s', label: '平均响应速度' }
]

const floatingCards = [
  { icon: '🚇', text: '线路查询', delay: 0 },
  { icon: '⚠️', text: '安全规范', delay: 0.5 },
  { icon: '📋', text: '技术标准', delay: 1 },
  { icon: '🔧', text: '故障诊断', delay: 1.5 }
]

const features = [
  {
    title: '智能问答',
    desc: '基于大模型与RAG技术，快速解答轨道交通专业问题',
    icon: ChatDotRound,
    path: '/chat',
    gradient: 'linear-gradient(135deg, #0EA5E9 0%, #38BDF8 100%)'
  },
  {
    title: '知识检索',
    desc: '全文检索与语义检索，精准定位行业标准与文档',
    icon: Document,
    path: '/knowledge/query',
    gradient: 'linear-gradient(135deg, #10B981 0%, #34D399 100%)'
  },
  {
    title: '组件管理',
    desc: '意图识别、问题切分、知识图谱等组件统一管理',
    icon: MapLocation,
    path: '/component',
    gradient: 'linear-gradient(135deg, #F59E0B 0%, #FBBF24 100%)'
  },
  {
    title: '文档查询',
    desc: '国标、行标与设计规范一站式查询与引用',
    icon: Collection,
    path: '/knowledge',
    gradient: 'linear-gradient(135deg, #8B5CF6 0%, #A78BFA 100%)'
  }
]
</script>

<template>
  <div class="home-view">
    <!-- Hero -->
    <section class="hero">
      <div class="hero-inner">
        <div class="hero-content">
          <el-tag class="hero-badge" effect="plain" type="primary">智能服务，专业解答</el-tag>
          <h1 class="hero-title">
            您的专属
            <span class="title-highlight">轨道交通行业知识助手</span>
          </h1>
          <p class="hero-desc">
            基于大语言模型与 RAG 技术，为轨道交通行业提供智能问答、知识检索与标准查询等一站式知识服务。
          </p>
          <div class="hero-actions">
            <el-button type="primary" class="action-button primary" @click="goConsult">
              <el-icon><ChatDotRound /></el-icon>
              开始提问
            </el-button>
            <el-button class="action-button secondary" @click="goKnowledge">
              知识库管理
            </el-button>
          </div>
        </div>
        <div class="hero-visual">
          <div
            v-for="(card, i) in floatingCards"
            :key="card.text"
            class="floating-card"
            :style="{ animationDelay: `${card.delay}s` }"
          >
            <span class="floating-icon">{{ card.icon }}</span>
            <span>{{ card.text }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 核心功能 -->
    <section class="section core-features">
      <h2 class="section-title">
        核心功能
        <span class="title-highlight">一站式解决轨道交通行业知识需求</span>
      </h2>
      <div class="feature-grid">
        <el-card
          v-for="item in features"
          :key="item.title"
          class="feature-card"
          shadow="hover"
          @click="goTo(item.path)"
        >
          <div class="feature-decoration" :style="{ background: item.gradient }"></div>
          <div class="feature-body">
            <div class="feature-icon-wrap" :style="{ background: item.gradient }">
              <el-icon :size="28"><component :is="item.icon" /></el-icon>
            </div>
            <h3 class="feature-title">{{ item.title }}</h3>
            <p class="feature-desc">{{ item.desc }}</p>
            <a class="feature-link" @click.stop="goTo(item.path)">
              了解更多
              <el-icon><ArrowRight /></el-icon>
            </a>
          </div>
        </el-card>
      </div>
    </section>

    <!-- 快速开始
    <section class="section quick-start">
      <el-card class="quick-start-card" shadow="hover">
        <div class="quick-start-inner">
          <div class="quick-start-visual">
            <svg viewBox="0 0 200 120" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="60" cy="60" r="40" fill="url(#bubble1)" opacity="0.9" />
              <circle cx="140" cy="50" r="30" fill="url(#bubble2)" opacity="0.8" />
              <path d="M100 90 L95 100 L105 100 Z" fill="var(--primary-400)" opacity="0.7" />
              <circle cx="160" cy="90" r="8" fill="var(--warning)" opacity="0.9" />
              <circle cx="40" cy="30" r="6" fill="var(--info)" opacity="0.8" />
              <defs>
                <linearGradient id="bubble1" x1="20" y1="20" x2="100" y2="100">
                  <stop stop-color="#0EA5E9" />
                  <stop offset="1" stop-color="#38BDF8" />
                </linearGradient>
                <linearGradient id="bubble2" x1="110" y1="20" x2="170" y2="80">
                  <stop stop-color="#38BDF8" />
                  <stop offset="1" stop-color="#7DD3FC" />
                </linearGradient>
              </defs>
            </svg>
          </div>
          <div class="quick-start-content">
            <h3 class="quick-title">快速开始</h3>
            <p class="quick-desc">点击下方常见问题，一键发起咨询</p>
            <div class="quick-chips">
              <el-tag
                v-for="q in quickQuestions"
                :key="q"
                class="quick-chip"
                effect="plain"
                @click="goConsult"
              >
                {{ q }}
              </el-tag>
            </div>
          </div>
        </div>
      </el-card>
    </section> -->

    <!-- 统计数据 -->
    <section class="section stats-section">
      <div class="stats-inner">
        <div v-for="s in stats" :key="s.label" class="stat-item">
          <span class="stat-value">{{ s.value }}</span>
          <span class="stat-label">{{ s.label }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.home-view {
  padding-bottom: 80px;
}

.hero {
  padding: 60px var(--padding-inline) 80px;
  min-height: 480px;
}

.hero-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 48px;
  align-items: center;
}

.hero-badge {
  margin-bottom: 16px;
  border-radius: 20px;
}

.hero-title {
  font-size: 40px;
  font-weight: 700;
  line-height: 1.25;
  color: var(--gray-900);
  margin-bottom: 16px;
}

.hero-desc {
  font-size: 16px;
  color: var(--gray-600);
  line-height: 1.6;
  margin-bottom: 28px;
  max-width: 480px;
}

.hero-actions {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}

.hero-visual {
  position: relative;
  height: 280px;
}

.floating-card {
  position: absolute;
  animation: float 3s ease-in-out infinite;
  background: var(--white);
  padding: 12px 20px;
  border-radius: var(--card-radius);
  box-shadow: var(--shadow-md);
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 500;
  color: var(--gray-900);
}

.floating-card:nth-child(1) {
  top: 10%;
  left: 10%;
}
.floating-card:nth-child(2) {
  top: 5%;
  right: 15%;
  animation-delay: 0.5s;
}
.floating-card:nth-child(3) {
  bottom: 25%;
  left: 20%;
  animation-delay: 1s;
}
.floating-card:nth-child(4) {
  bottom: 15%;
  right: 10%;
  animation-delay: 1.5s;
}

.floating-icon {
  font-size: 20px;
}

.section {
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 0 var(--padding-inline) 64px;
}

.section-title {
  font-size: 28px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 32px;
  line-height: 1.4;
}

.core-features .feature-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--grid-gap);
}

.feature-card {
  border-radius: var(--card-radius-lg);
  overflow: hidden;
  position: relative;
  transition: var(--transition-normal);
  cursor: pointer;

  :deep(.el-card__body) {
    padding: var(--padding-block) var(--padding-inline);
  }

  &:hover {
    transform: translateY(-4px);
    box-shadow: var(--shadow-hover);
  }
}

.feature-decoration {
  position: absolute;
  width: 120px;
  height: 120px;
  border-radius: 50%;
  top: -40px;
  right: -40px;
  opacity: 0.15;
}

.feature-body {
  position: relative;
}

.feature-icon-wrap {
  width: 56px;
  height: 56px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  margin-bottom: 16px;
}

.feature-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 8px;
}

.feature-desc {
  font-size: 14px;
  color: var(--gray-600);
  line-height: 1.5;
  margin-bottom: 12px;
}

.feature-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  color: var(--primary-500);
  text-decoration: none;
  font-weight: 500;
  cursor: pointer;
}

.quick-start-card {
  border-radius: var(--card-radius-lg);
  background: var(--primary-gradient);
  border: none;

  :deep(.el-card__body) {
    padding: 32px;
  }
}

.quick-start-inner {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 40px;
  align-items: center;
}

.quick-start-visual svg {
  width: 100%;
  height: auto;
}

.quick-start-content {
  color: var(--white);
}

.quick-title {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 8px;
}

.quick-desc {
  font-size: 14px;
  opacity: 0.95;
  margin-bottom: 20px;
}

.quick-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.quick-chip {
  cursor: pointer;
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.5);
  color: white;

  &:hover {
    background: rgba(255, 255, 255, 0.3);
  }
}

.stats-section {
  padding-top: 24px;
}

.stats-inner {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 24px;
  padding: 32px;
  background: var(--bg-card);
  border-radius: var(--card-radius-lg);
  box-shadow: var(--shadow-sm);
}

.stat-item {
  text-align: center;
}

.stat-value {
  display: block;
  font-size: 32px;
  font-weight: 700;
  color: var(--primary-500);
  margin-bottom: 4px;
}

.stat-label {
  font-size: 13px;
  color: var(--gray-500);
}

@media (max-width: 1024px) {
  .hero-inner {
    grid-template-columns: 1fr;
    text-align: center;
  }
  .hero-desc {
    margin-left: auto;
    margin-right: auto;
  }
  .hero-actions {
    justify-content: center;
  }
  .hero-visual {
    height: 200px;
  }
  .core-features .feature-grid {
    grid-template-columns: 1fr;
  }
  .quick-start-inner {
    grid-template-columns: 1fr;
  }
  .quick-start-visual {
    max-width: 160px;
    margin: 0 auto;
  }
  .stats-inner {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .hero {
    padding: 40px 20px 60px;
  }
  .hero-title {
    font-size: 28px;
  }
  .hero-actions {
    flex-direction: column;
  }
  .hero-actions .el-button {
    width: 100%;
  }
  .floating-card {
    font-size: 12px;
    padding: 8px 14px;
  }
  .section-title {
    font-size: 22px;
  }
  .stats-inner {
    grid-template-columns: 1fr;
    padding: 24px;
  }
}
</style>
