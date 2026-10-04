<script setup lang="ts">
import { onMounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import JsonResult from "../../../components/JsonResult.vue"
type TestCase = { id: string; source_id: string; title: string; source: string; expected: number | null }
const { output, error, busy, run } = useAction()
const cases = ref<TestCase[]>([]), caseId = ref(""), candidateId = ref(""), digest = ref(""), runId = ref("")
const csv = ref("source_id,source_revision,title,tenant,status,expected_total\nnew-case,1,订单查询,tenant-a,completed,3")
async function load() { cases.value = await api<TestCase[]>("/internal/test-cases"); return cases.value }
async function candidate() { const result = await api<{ id: string; digest: string }>(`/internal/test-cases/${caseId.value}/candidate`, "POST"); candidateId.value = result.id; digest.value = result.digest; return result }
async function execute() { const result = await api<{ run_id: string }>(`/internal/test-candidates/${candidateId.value}/run`, "POST"); runId.value = result.run_id; return result }
onMounted(() => run(load))
</script>
<template>
  <section><h2>用例与来源</h2><button @click="run(load)">刷新用例</button><div class="row"><button v-for="c in cases" :key="c.id" :class="{ active: c.id === caseId }" @click="caseId = c.id">{{ c.title }} · 预期 {{ c.expected ?? '待核验' }} · {{ c.source }}</button></div><textarea v-model="csv" aria-label="CSV 用例"></textarea><button @click="run(() => api('/internal/test-cases/import', 'POST', { csv }))">导入 CSV</button></section>
  <section><h2>固定脚本候选与实测断言</h2><div class="row"><label>用例 <input v-model="caseId"></label><button :disabled="!caseId || busy" @click="run(candidate)">生成候选</button></div><div class="row"><label>候选 <input v-model="candidateId"></label><label>摘要 <input v-model="digest"></label><button :disabled="!candidateId" @click="run(() => api(`/internal/test-candidates/${candidateId}/approve`, 'POST', { digest }))">批准摘要</button><button :disabled="!candidateId" @click="run(execute)">隔离运行</button></div><div class="row"><label>运行 <input v-model="runId"></label><button :disabled="!runId" @click="run(() => api(`/internal/test-runs/${runId}`))">读取报告</button></div><p class="muted">自然语言预期与实际观察分别保存；未经审批的脚本不执行。</p></section>
  <JsonResult :value="output" :error="error" :busy="busy" />
</template>
