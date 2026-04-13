<script setup>
defineProps({
  eyebrow: { type: String, default: '' },
  title: { type: String, required: true },
  subtitle: { type: String, default: '' }
})
</script>

<template>
  <div class="auth-shell">
    <div class="auth-bg" aria-hidden="true">
      <span class="orb orb-1" />
      <span class="orb orb-2" />
      <span class="orb orb-3" />
      <div class="auth-grid" />
    </div>

    <div class="auth-inner">
      <router-link to="/" class="brand-block">
        <div class="brand-mark">
          <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <rect width="40" height="40" rx="10" fill="url(#authShellGrad)" />
            <path d="M12 20h16M12 14h10M12 26h14" stroke="white" stroke-width="2" stroke-linecap="round" />
            <defs>
              <linearGradient id="authShellGrad" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
                <stop stop-color="#0EA5E9" />
                <stop offset="1" stop-color="#38BDF8" />
              </linearGradient>
            </defs>
          </svg>
        </div>
        <div class="brand-text">
          <span class="brand-zh">轨道交通知识服务系统</span>
          <span class="brand-en">Rail Transit Knowledge Service</span>
        </div>
      </router-link>

      <div class="auth-card">
        <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
        <h1 class="auth-title">{{ title }}</h1>
        <p v-if="subtitle" class="auth-subtitle">{{ subtitle }}</p>
        <div class="auth-body">
          <slot />
        </div>
        <div v-if="$slots.footer" class="auth-footer">
          <slot name="footer" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.auth-shell {
  position: relative;
  min-height: calc(100vh - var(--nav-height));
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px 48px;
  overflow: hidden;
}

.auth-bg {
  position: absolute;
  inset: 0;
  background: linear-gradient(165deg, #f0f9ff 0%, #f8fafc 42%, #eff6ff 100%);
  pointer-events: none;
}

.auth-grid {
  position: absolute;
  inset: 0;
  opacity: 0.35;
  background-image:
    linear-gradient(rgba(14, 165, 233, 0.06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(14, 165, 233, 0.06) 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(ellipse 80% 70% at 50% 30%, black, transparent);
}

.orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(64px);
  opacity: 0.55;
}

.orb-1 {
  width: 320px;
  height: 320px;
  background: #7dd3fc;
  top: -120px;
  right: -80px;
}

.orb-2 {
  width: 280px;
  height: 280px;
  background: #bae6fd;
  bottom: -100px;
  left: -60px;
}

.orb-3 {
  width: 200px;
  height: 200px;
  background: #a5f3fc;
  top: 40%;
  left: 35%;
  opacity: 0.25;
}

.auth-inner {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 440px;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 28px;
}

.brand-block {
  display: flex;
  align-items: center;
  gap: 14px;
  text-decoration: none;
  color: inherit;
  padding: 0 4px;
  transition: opacity var(--transition-fast);
}

.brand-block:hover {
  opacity: 0.88;
}

.brand-mark {
  width: 44px;
  height: 44px;
  flex-shrink: 0;
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(14, 165, 233, 0.25);
}

.brand-mark svg {
  display: block;
  width: 100%;
  height: 100%;
}

.brand-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.brand-zh {
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900);
  letter-spacing: -0.02em;
}

.brand-en {
  font-size: 11px;
  color: var(--gray-500);
  letter-spacing: 0.02em;
}

.auth-card {
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 20px;
  padding: 32px 32px 28px;
  border: 1px solid rgba(226, 232, 240, 0.9);
  box-shadow:
    0 4px 24px rgba(15, 23, 42, 0.06),
    0 0 0 1px rgba(255, 255, 255, 0.8) inset;
}

.eyebrow {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--primary-600);
}

.auth-title {
  margin: 0 0 10px;
  font-size: 26px;
  font-weight: 700;
  color: var(--gray-900);
  letter-spacing: -0.03em;
  line-height: 1.2;
}

.auth-subtitle {
  margin: 0 0 28px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--gray-600);
}

.auth-body {
  margin-bottom: 4px;
}

.auth-footer {
  margin-top: 24px;
  padding-top: 22px;
  border-top: 1px solid var(--gray-200);
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 8px 12px;
  font-size: 14px;
  color: var(--gray-600);
}

@media (max-width: 480px) {
  .auth-card {
    padding: 26px 22px 22px;
    border-radius: 16px;
  }

  .auth-title {
    font-size: 22px;
  }

  .brand-en {
    display: none;
  }
}
</style>
