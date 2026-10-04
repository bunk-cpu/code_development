<script setup lang="ts">
import { onMounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import JsonResult from "../../../components/JsonResult.vue"
const { output, error, busy, run } = useAction()
const versions = ref<Array<{ version: string; digest: string; status: string }>>([]), version = ref("0.2.0"), digest = ref("")
const tenant = ref("tenant-a"), release = ref("v1"), deploymentRevision = ref(1), enabled = ref(true), sourceSnapshot = ref("")
async function load() { versions.value = await api<typeof versions.value>("/internal/workflow-versions"); return versions.value }
async function create() { return api("/internal/workflow-versions", "POST", { workflow_id: "order.summary.read", version: version.value, risk: "R0_read_only", description: "读取本租户订单状态统计", template_id: "api.order_summary.read", releases: ["v1", "v2"], input_schema: { status: ["all", "pending", "completed"], date_from: "date", date_to: "date" } }) }
onMounted(() => run(load))
</script>
<template><section><h2>流程工厂</h2><button @click="run(load)">读取规范版本</button><div class="row"><button v-for="v in versions" :key="v.version" @click="version = v.version; digest = v.digest">{{ v.version }} · {{ v.status }}</button></div><div class="row"><label>版本 <input v-model="version"></label><label>摘要 <input v-model="digest"></label><button @click="run(create)">新建规范</button><button @click="run(() => api(`/internal/workflow-versions/${version}/validate`, 'POST'))">双租户验证</button><button v-for="action in ['approve', 'publish', 'disable']" :key="action" @click="run(() => api(`/internal/workflow-versions/${version}/${action}`, 'POST', { digest }))">{{ { approve: '批准', publish: '发布', disable: '停用' }[action] }}</button></div></section>
<section><h2>实际部署与功能开关</h2><button @click="run(() => api('/internal/deployments'))">查看部署</button><div class="row"><label>租户 <select v-model="tenant"><option>tenant-a</option><option>tenant-b</option></select></label><label>产品版本 <select v-model="release"><option>v1</option><option>v2</option></select></label><label>已部署源码快照 ID <input v-model="sourceSnapshot"></label><label>当前部署修订 <input v-model.number="deploymentRevision" type="number" min="1"></label><label><input v-model="enabled" type="checkbox">开放订单统计</label><button @click="run(() => api('/internal/deployments', 'POST', { tenant, release, snapshot_id: sourceSnapshot || null, flags: { 'order.summary.read': enabled }, expected_revision: deploymentRevision }))">登记生效配置</button></div></section>
<section><h2>运行与质量</h2><div class="row"><button @click="run(() => api('/internal/jobs'))">我的后台任务</button><button @click="run(() => api('/internal/metrics'))">质量与运行指标</button><button @click="run(() => api('/internal/audit'))">审核审计</button></div></section><JsonResult :value="output" :error="error" :busy="busy" /></template>
