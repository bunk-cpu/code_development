<script setup lang="ts">
import { ref } from "vue"
import LoginPanel from "../../components/LoginPanel.vue"
import { useSession } from "../../composables/useSession"
import SourceView from "./views/SourceView.vue"
import ManualsView from "./views/ManualsView.vue"
import TestStudioView from "./views/TestStudioView.vue"
import TestPilotView from "./views/TestPilotView.vue"
import OperationsView from "./views/OperationsView.vue"
import RuntimeView from "./views/RuntimeView.vue"
const { user, login, logout } = useSession("internal")
const tabs = { source: { name: "源码与功能", view: SourceView }, manuals: { name: "手册审核", view: ManualsView },
  tests: { name: "测试工作室", view: TestStudioView }, testpilot: { name: "TestPilot", view: TestPilotView },
  operations: { name: "问题运营", view: OperationsView }, runtime: { name: "流程与运行", view: RuntimeView } }
const tab = ref<keyof typeof tabs>("source")
</script>
<template>
  <main><header><p class="eyebrow">SOURCE KNOWLEDGE</p><h1>源码知识工作台</h1><p class="muted">固定版本的证据，经过审核的知识，可验证的执行。</p></header>
    <LoginPanel audience="internal" :user="user" :login="login" :logout="logout" />
    <template v-if="user"><nav class="tabs" aria-label="工作台导航"><button v-for="(item, key) in tabs" :key="key" :aria-current="tab === key ? 'page' : undefined" :class="{ active: tab === key }" @click="tab = key">{{ item.name }}</button><a href="/docs">API 文档</a></nav>
      <KeepAlive><component :is="tabs[tab].view" :key="`${user.name}:${tab}`" /></KeepAlive>
    </template>
  </main>
</template>
