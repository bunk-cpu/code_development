<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import type { Execution } from "../../../types"
const { error, busy, run } = useAction()
const tasks = ref<Execution[]>([]), selected = ref<Execution | null>(null)
const labels: Record<string, string> = { queued: "等待查询", running: "正在查询", succeeded: "查询完成", failed: "查询失败", cancelled: "已取消" }
async function load() { tasks.value = await api<Execution[]>("/customer/executions"); if (selected.value) selected.value = await api<Execution>(`/customer/executions/${selected.value.id}`) }
async function inspect(id: string) { selected.value = await api<Execution>(`/customer/executions/${id}`) }
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { run(load); timer = setInterval(() => { if (tasks.value.some(t => ['queued', 'running'].includes(t.status))) run(load) }, 2000) })
onUnmounted(() => clearInterval(timer))
</script>
<template><section><h2>我的任务</h2><button :disabled="busy" @click="run(load)">刷新任务</button><p v-if="error" role="alert" class="notice">{{ error }}</p><p v-if="!tasks.length && !busy">暂无任务，可在常用流程发起查询。</p><ul class="task-list"><li v-for="t in tasks" :key="t.id"><span>订单统计查询 · {{ labels[t.status] ?? '处理中' }}</span><button @click="run(() => inspect(t.id))">查看结果</button></li></ul><article v-if="selected"><h3>{{ labels[selected.status] }}</h3><p v-if="selected.result?.summary">符合条件的订单共 <strong>{{ selected.result.summary.total }}</strong> 笔。</p><p v-if="selected.status === 'failed'">查询暂未完成，请稍后重新查询或联系支持人员。</p><button v-if="['queued', 'running'].includes(selected.status)" @click="run(() => api(`/customer/executions/${selected!.id}/cancel`, 'POST'))">取消查询</button></article></section></template>
