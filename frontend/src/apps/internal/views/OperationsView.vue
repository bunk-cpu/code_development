<script setup lang="ts">
import { onMounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import JsonResult from "../../../components/JsonResult.vue"
const { output, error, busy, run } = useAction()
const topic = ref("订单统计"), kind = ref("knowledge_gap"), owner = ref("analyst"), note = ref("")
onMounted(() => run(() => api("/internal/operations")))
</script>
<template><section><h2>问题记录与流程候选</h2><button @click="run(() => api('/internal/operations'))">刷新脱敏问题、反馈及候选</button><div class="row"><label>主题 <input v-model="topic"></label><label>待办类型 <select v-model="kind"><option value="knowledge_gap">知识缺口</option><option value="incorrect_answer">错误答案</option><option value="workflow_candidate">流程候选</option></select></label><label>负责人 <input v-model="owner"></label></div><textarea v-model="note" aria-label="待办说明"></textarea><button @click="run(() => api('/internal/operations/tasks', 'POST', { topic, kind, owner, note }))">创建待办</button><p class="muted">用户反馈进入审核待办，不自动改写正式知识。</p></section><JsonResult :value="output" :error="error" :busy="busy" /></template>
