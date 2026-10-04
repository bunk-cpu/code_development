<script setup lang="ts">
import { ref } from "vue"
import LoginPanel from "../../components/LoginPanel.vue"
import { useSession } from "../../composables/useSession"
import HelpView from "./views/HelpView.vue"
import AssistantView from "./views/AssistantView.vue"
import WorkflowsView from "./views/WorkflowsView.vue"
import TasksView from "./views/TasksView.vue"
const { user, login, logout } = useSession("customer")
const tabs = { help: { name: "产品帮助", view: HelpView }, assistant: { name: "问答助手", view: AssistantView },
  workflows: { name: "常用流程", view: WorkflowsView }, tasks: { name: "我的任务", view: TasksView } }
const tab = ref<keyof typeof tabs>("help")
</script>
<template><main><header><p class="eyebrow">PRODUCT ASSISTANT</p><h1>产品服务中心</h1><p class="muted">查找帮助，解决问题，完成日常操作。</p></header><LoginPanel audience="customer" :user="user" :login="login" :logout="logout" /><template v-if="user"><nav class="tabs" aria-label="客户服务导航"><button v-for="(item, key) in tabs" :key="key" :aria-current="tab === key ? 'page' : undefined" :class="{ active: tab === key }" @click="tab = key">{{ item.name }}</button></nav><component :is="tabs[tab].view" :key="`${user.name}:${tab}`" /></template></main></template>
