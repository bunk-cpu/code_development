<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import JsonResult from "../../../components/JsonResult.vue"
import type { Snapshot, SourceAnswer } from "../../../types"
type Index = { manifest: { modules: Array<{ id: string; artifact: string }> }; report: unknown; status: string }
type AgentRun = { id: string; snapshot_id: string; module?: string; status: string; stage: string; bundle_hash: string; bundle?: { draft: { claims: Array<{ claim_id: string; text: string; evidence_ids: string[] }>; open_questions: string[] } }; events?: Array<{ id: number; stage: string }> }
const { output, error, busy, run } = useAction()
const snapshots = ref<Snapshot[]>([]), snapshot = ref(""), moduleId = ref("root")
const index = ref<Index | null>(null), question = ref("订单统计入口和权限在哪里"), answer = ref<SourceAnswer | null>(null)
const repositoryName = ref("Java 业务项目"), repositoryRoot = ref("/root/code_development/fixtures/v2"), repositoryId = ref(""), gitRef = ref("HEAD"), jobId = ref("")
const runs = ref<AgentRun[]>([]), selectedRun = ref<AgentRun | null>(null), answerId = ref("")
const selectedClaims = ref<string[]>([])
const base = ref(""), featureId = ref("order.summary"), featureTitle = ref("订单统计")
const targetRelease = ref("v2"), classpath = ref(""), javaRelease = ref("17"), materialKind = ref("manual"), materialTarget = ref("")
let events: EventSource | null = null
async function refresh() { snapshots.value = await api<Snapshot[]>("/internal/source/snapshots"); if (!snapshot.value) snapshot.value = snapshots.value[0]?.id ?? ""; runs.value = await api<AgentRun[]>("/internal/source/analysis-runs"); return snapshots.value }
watch(snapshot, async () => {
  answer.value = null; answerId.value = ""; index.value = null
  if (!snapshot.value || !snapshots.value.find(s => s.id === snapshot.value)?.repository_id) return
  const id = snapshot.value
  try { const result = await api<Index>(`/internal/source/${id}/index`); if (snapshot.value === id) { index.value = result; moduleId.value = result.manifest.modules[0]?.id ?? "root" } }
  catch (cause) { error.value = cause instanceof Error ? cause.message : "索引读取失败" }
})
async function register() { const row = await api<{ id: string }>("/internal/repositories", "POST", { name: repositoryName.value, root: repositoryRoot.value, classpath: classpath.value.split(/[,\n]/).map(p => p.trim()).filter(Boolean), java_release: javaRelease.value }); repositoryId.value = row.id; return row }
async function indexRepository() { const job = await api<{ id: string }>(`/internal/repositories/${repositoryId.value}/index`, "POST", { ref: gitRef.value }); jobId.value = job.id; return job }
async function ask() { const id = snapshot.value; const response = await api<SourceAnswer>(`/internal/source/${id}/ask`, "POST", { question: question.value }); if (snapshot.value === id) { answer.value = response; answerId.value = response.answer_id ?? "" }; return response }
async function createAnswer() { const response = await api<{ answer_id: string; job_id: string }>("/internal/code-answers", "POST", { snapshot_id: snapshot.value, question: question.value }, { "Idempotency-Key": crypto.randomUUID() }); answerId.value = response.answer_id; jobId.value = response.job_id; return response }
async function analyze() { const response = await api<{ run_id: string; job_id: string }>("/internal/source/analysis-runs", "POST", { snapshot_id: snapshot.value, module_id: moduleId.value }); jobId.value = response.job_id; await refresh(); return response }
async function inspect(id: string) {
  events?.close()
  selectedClaims.value = []
  selectedRun.value = await api<AgentRun>(`/internal/source/analysis-runs/${id}`)
  if (["QUEUED", "RUNNING"].includes(selectedRun.value.status)) {
    events = new EventSource(`/internal/source/analysis-runs/${id}/events`)
    events.addEventListener("progress", event => {
      const progress = JSON.parse((event as MessageEvent).data)
      if (selectedRun.value?.id === id) Object.assign(selectedRun.value, progress)
      if (!["QUEUED", "RUNNING"].includes(progress.status)) {
        events?.close()
        api<AgentRun>(`/internal/source/analysis-runs/${id}`).then(r => { if (selectedRun.value?.id === id) selectedRun.value = r }).catch(() => { error.value = "进度连接中断，请刷新分析结果" })
      }
    })
    events.onerror = () => { events?.close(); error.value = "进度连接中断，请重新读取分析结果" }
  }
  return selectedRun.value
}
async function review(decision: string) { if (!selectedRun.value) return; return api(`/internal/source/analysis-runs/${selectedRun.value.id}/review`, "POST", { digest: selectedRun.value.bundle_hash, decision, comment: decision === "reject" ? "需重新核验主张及证据" : "已核对逐条主张的适用条件" }) }
async function confirmFeature() { if (!selectedRun.value?.bundle || !selectedClaims.value.length) throw new Error("请选择获批画像中支持该功能的主张"); return api("/internal/features/confirm", "POST", { run_id: selectedRun.value.id, feature_id: featureId.value, title: featureTitle.value, claim_ids: selectedClaims.value }) }
async function generateManual() { if (!answer.value?.citations.length) throw new Error("请先取得源码证据"); return api('/internal/manuals/from-evidence', 'POST', { page_id: featureId.value, release: targetRelease.value, title: featureTitle.value, snapshot_id: answer.value.snapshot_id, evidence_ids: answer.value.citations.slice(0, 3).map(c => c.id) }) }
onMounted(() => run(refresh))
onDeactivated(() => events?.close())
onActivated(() => { if (selectedRun.value && ["QUEUED", "RUNNING"].includes(selectedRun.value.status)) run(() => inspect(selectedRun.value!.id)) })
onUnmounted(() => events?.close())
</script>
<template>
  <section><h2>项目与来源</h2><div class="row"><label>项目名称 <input v-model="repositoryName"></label><label>本机目录 <input v-model="repositoryRoot" size="45"></label><button :disabled="busy" @click="run(register)">登记仓库</button></div><div class="row"><label>JDK release <select v-model="javaRelease"><option>8</option><option>11</option><option>17</option><option>21</option></select></label><label>依赖 JAR 绝对路径（逗号分隔）<input v-model="classpath" size="45"></label><label>仓库 ID <input v-model="repositoryId"></label><label>Git ref <input v-model="gitRef"></label><button :disabled="busy || !repositoryId" @click="run(indexRepository)">创建索引任务</button><button @click="run(() => api('/internal/repositories'))">仓库列表</button></div></section>
  <section><h2>固定版本与源码问答</h2><div class="row"><button @click="run(refresh)">刷新快照</button><label>源码快照 <select v-model="snapshot"><option v-for="s in snapshots" :key="s.id" :value="s.id">{{ s.version.slice(0, 60) }} · {{ s.coverage }}</option></select></label></div><p v-if="index" class="muted">JDT 索引：{{ index.status }}；静态事实与实际部署分别核验。</p>
    <form class="row" @submit.prevent="run(ask)"><label>源码问题 <input v-model="question" size="45" required></label><button :disabled="busy || !snapshot" class="primary">查询源码</button><button type="button" :disabled="!index || busy" @click="run(createAnswer)">后台问答任务</button></form>
    <article v-if="answer"><p>{{ answer.answer }}</p><div class="row"><button v-for="c in answer.citations" :key="c.id" @click="run(() => api(`/internal/source/${answer!.snapshot_id}/evidence/${c.id}`))">{{ c.file }}:{{ c.line }}</button></div></article>
    <div class="row"><label>草稿目标版本 <select v-model="targetRelease"><option>v1</option><option>v2</option></select></label><button :disabled="!answer?.citations.length" @click="run(generateManual)">用证据生成手册草稿</button></div><div v-if="answerId" class="row"><button @click="run(() => api(`/internal/code-answers/${answerId}`))">读取后台答案</button><button @click="run(() => api(`/internal/code-answers/${answerId}/feedback`, 'POST', { kind: 'insufficient_evidence', comment: '需补全证据路径' }))">反馈证据不足</button></div>
  </section>
  <section><h2>模块分析 Agent 与功能地图</h2><div class="row"><label>模块 <select v-model="moduleId"><option v-for="m in index?.manifest.modules ?? []" :key="m.id" :value="m.id">{{ m.id }} · {{ m.artifact }}</option></select></label><button :disabled="!index || busy" @click="run(analyze)">启动 DeepSeek 分析</button><button @click="run(refresh)">刷新分析列表</button><button :disabled="!index" @click="run(() => api(`/internal/features?snapshot_id=${snapshot}`))">查看功能地图</button></div>
    <div class="row"><button v-for="r in runs" :key="r.id" @click="run(() => inspect(r.id))">{{ r.module ?? r.id }} · {{ r.status }}</button></div>
    <article v-if="selectedRun"><p>阶段：{{ selectedRun.stage }} · {{ selectedRun.status }}</p><ol><li v-for="c in selectedRun.bundle?.draft.claims ?? []" :key="c.claim_id"><label><input v-model="selectedClaims" type="checkbox" :value="c.claim_id">{{ c.text }}</label> <button v-for="eid in c.evidence_ids" :key="eid" @click="run(() => api(`/internal/source/${selectedRun!.snapshot_id}/evidence/${eid}`))">来源</button></li></ol><div class="row"><button :disabled="selectedRun.status !== 'WAITING_REVIEW'" @click="run(() => review('approve'))">认可内部画像</button><button :disabled="selectedRun.status !== 'WAITING_REVIEW'" @click="run(() => review('reject'))">驳回</button><button @click="run(() => api(`/internal/source/analysis-runs/${selectedRun!.id}/cancel`, 'POST'))">取消分析</button></div><div class="row"><label>功能 ID <input v-model="featureId"></label><label>功能名称 <input v-model="featureTitle"></label><button :disabled="!selectedClaims.length || selectedRun.status !== 'COMPLETED'" @click="run(confirmFeature)">确认功能映射</button></div></article>
  </section>
  <section><h2>经审核的模块资产关联</h2><div class="row"><label>资产类型 <select v-model="materialKind"><option value="manual">已批准手册</option><option value="test">平台测试运行</option><option value="legacy_test">TestPilot 登记运行</option></select></label><label>目标资产 ID <input v-model="materialTarget"></label><button :disabled="!index || !materialTarget || busy" @click="run(() => api('/internal/source/material-bindings', 'POST', { snapshot_id: snapshot, module_id: moduleId, kind: materialKind, target_id: materialTarget }))">登记模块映射</button></div><p class="muted">资产映射记录审核者的关联判断；同构建、角色和环境下的业务实测须另行核验。</p></section>
  <section><h2>变更中心与任务</h2><div class="row"><label>基线 <select v-model="base"><option v-for="s in snapshots" :key="s.id" :value="s.id">{{ s.id }}</option></select></label><button :disabled="!base || !index" @click="run(() => api(`/internal/source/changes/compare?base=${base}&target=${snapshot}`))">比较变更影响</button></div><div class="row"><label>任务 ID <input v-model="jobId"></label><button :disabled="!jobId" @click="run(() => api(`/internal/jobs/${jobId}`))">读取任务进度</button></div></section>
  <JsonResult :value="output" :error="error" :busy="busy" />
</template>
