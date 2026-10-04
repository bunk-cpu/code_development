<script setup lang="ts">
import { onMounted, ref, watch } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
const { error, busy, run } = useAction()
type Preparation = { prepared_id: string; preview_digest: string; preview: { action: string; scope: string; changes_business_data: boolean }; expires_at: number }
const catalog = ref<Array<{ workflow_id: string; description: string; release: string }>>([])
const status = ref("all"), dateFrom = ref("2026-09-01"), dateTo = ref("2026-09-07"), prepared = ref<Preparation | null>(null)
const key = ref(""), message = ref("")
watch([status, dateFrom, dateTo], () => { prepared.value = null; message.value = "" })
async function preview() { prepared.value = null; prepared.value = await api<Preparation>("/customer/workflows/order.summary.read/prepare", "POST", { parameters: { status: status.value, date_from: dateFrom.value, date_to: dateTo.value } }); key.value = crypto.randomUUID() }
async function confirm() { if (!prepared.value) return; await api("/customer/executions", "POST", { prepared_id: prepared.value.prepared_id, confirmed_preview_digest: prepared.value.preview_digest, confirmation: true }, { "Idempotency-Key": key.value }); message.value = "查询已提交，可在“我的任务”查看结果。" }
onMounted(() => run(async () => { catalog.value = await api<typeof catalog.value>("/customer/workflows") }))
</script>
<template><section><h2>常用流程</h2><p v-if="!catalog.length && !busy">当前没有开放的流程。</p><template v-if="catalog.length"><h3>订单状态统计</h3><p>读取当前租户的订单数量，最多查询 31 天。</p><div class="row"><label>订单状态 <select v-model="status"><option value="all">全部</option><option value="pending">待处理</option><option v-if="catalog[0]?.release === 'v2'" value="completed">已完成</option></select></label><label>开始日期 <input v-model="dateFrom" type="date" required></label><label>结束日期 <input v-model="dateTo" type="date" required></label></div><button :disabled="busy" @click="run(preview)">预览操作</button><article v-if="prepared" class="preview"><h3>{{ prepared.preview.action }}</h3><p>{{ dateFrom }} 至 {{ dateTo }}，状态：{{ status }}</p><p>{{ prepared.preview.scope }}。此次查询不修改业务数据。</p><button class="primary" :disabled="busy || !!message" @click="run(confirm)">确认查询</button></article></template><p v-if="error" role="alert" class="notice">{{ error }}</p><p class="success" role="status">{{ message }}</p></section></template>
