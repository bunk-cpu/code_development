<script setup lang="ts">
import { ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import type { CustomerAnswer } from "../../../types"
const { error, busy, run } = useAction()
const question = ref("订单统计支持哪些状态？"), answer = ref<CustomerAnswer | null>(null), feedback = ref("")
async function ask() { feedback.value = ""; answer.value = await api<CustomerAnswer>("/customer/ask", "POST", { question: question.value }) }
async function rate(kind: string) { if (!answer.value) return; await api(`/customer/questions/${answer.value.question_id}/feedback`, "POST", { kind, comment: "" }); feedback.value = "反馈已收到，支持人员会核查。" }
</script>
<template><section><h2>问答助手</h2><form class="row" @submit.prevent="run(ask)"><label class="grow">您的问题 <input v-model="question" required minlength="2" maxlength="1000" placeholder="描述您想完成的任务"></label><button class="primary" :disabled="busy">{{ busy ? '正在查找…' : '提问' }}</button></form><p v-if="error" role="alert" class="notice">{{ error }}</p><article v-if="answer"><span class="badge">适用于 {{ answer.release }}</span><p class="manual-content">{{ answer.answer }}</p><p v-if="answer.citations.length" class="muted">参考帮助：<a v-for="c in answer.citations" :key="c.page_id" :href="`/customer-ui?page=${encodeURIComponent(c.page_id)}&release=${encodeURIComponent(answer!.release)}`">{{ c.title }} </a></p><div class="row"><button :disabled="busy" @click="run(() => rate('accurate'))">有帮助</button><button :disabled="busy" @click="run(() => rate('incorrect'))">需要核查</button></div><p role="status">{{ feedback }}</p></article></section></template>
