<script setup lang="ts">
import { onMounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import JsonResult from "../../../components/JsonResult.vue"
import type { Manual } from "../../../types"
const { output, error, busy, run } = useAction()
const release = ref("v1"), rows = ref<Manual[]>([]), selected = ref<Manual | null>(null)
const pageId = ref("order-summary"), title = ref("订单统计"), content = ref("# 订单统计\n\n请填写经过核验的操作说明。")
const documentName = ref("manual.md"), patchId = ref(""), patchText = ref(""), rollbackId = ref(0), head = ref(0)
async function load() { rows.value = await api<Manual[]>(`/internal/manuals?release=${release.value}`); return rows.value }
function select(m: Manual) { selected.value = m; pageId.value = m.page_id; title.value = m.title; content.value = m.content }
async function save() { const m = await api<Manual>("/internal/manuals", selected.value ? "PUT" : "POST", { page_id: pageId.value, release: release.value, title: title.value, content: content.value, ...(selected.value ? { baseline_id: selected.value.id } : {}) }); select(m); await load(); return m }
async function review(decision: string) { if (!selected.value) return; const r = await api(`/internal/manuals/${selected.value.id}/review`, "POST", { digest: selected.value.digest, decision, comment: "已核对内容与适用版本" }); await load(); return r }
async function patch() { if (!selected.value) return; const baseline = await api<{ blocks: Array<{ block_id: string; hash: string }> }>(`/internal/manuals/${selected.value.id}/blocks`); const b = baseline.blocks[0]!; const response = await api<{ patch_id: string }>("/internal/manuals/patches", "POST", { baseline_id: selected.value.id, baseline_hash: selected.value.digest, operations: [{ operation: "replace", block_id: b.block_id, expected_hash: b.hash, new_markdown: patchText.value }], evidence_ids: [] }); patchId.value = response.patch_id; return response }
async function history() { const result = await api<Array<{ id: number; current: boolean }>>(`/internal/knowledge/${release.value}/history`); head.value = result.find(r => r.current)?.id ?? 0; return result }
async function upload(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; if (file.size > 1000000) throw new Error("文档最多 1 MB"); documentName.value = file.name; content.value = await file.text(); return api('/internal/manuals/import', 'POST', { release: release.value, name: file.name, content: content.value }) }
onMounted(() => run(load))
</script>
<template>
  <section><h2>版本手册与导入</h2><label>上传 Markdown / 文本 <input type="file" accept=".md,.txt" @change="run(() => upload($event))"></label><div class="row"><label>产品版本 <select v-model="release" @change="selected = null"><option>v1</option><option>v2</option></select></label><button @click="run(load)">读取修订</button><button @click="selected = null">新建章节</button><label>文档名 <input v-model="documentName"></label><button @click="run(() => api('/internal/manuals/import', 'POST', { release, name: documentName, content }))">将正文导入文档</button></div><div class="row"><button v-for="m in rows" :key="m.id" @click="select(m)">{{ m.title }} · #{{ m.id }} · {{ m.status }}</button></div></section>
  <section><h2>编辑与摘要审核</h2><div class="row"><label>页面 ID <input v-model="pageId"></label><label>标题 <input v-model="title"></label></div><textarea v-model="content" aria-label="手册正文" rows="12"></textarea><p v-if="selected" class="muted">编辑基线 #{{ selected.id }}；发生并发冲突时保留输入内容。</p><div class="row"><button :disabled="busy" class="primary" @click="run(save)">保存新修订</button><button :disabled="!selected" @click="run(() => review('approve'))">批准选定摘要</button><button :disabled="!selected" @click="run(() => review('reject'))">驳回修订</button></div></section>
  <section><h2>段落补丁与差异</h2><textarea v-model="patchText" aria-label="首段替换内容" placeholder="首段替换内容；生成后查看差异，确认再应用"></textarea><button :disabled="!selected" @click="run(patch)">生成首段补丁</button><button :disabled="!patchId" @click="run(() => api(`/internal/manuals/patches/${patchId}/apply`, 'POST'))">应用补丁</button></section>
  <section><h2>完整知识发布与回滚</h2><div class="row"><button @click="run(() => api(`/internal/knowledge/${release}/publish`, 'POST'))">发布已批准完整快照</button><button @click="run(history)">发布历史</button><label>回滚快照 <input v-model.number="rollbackId" type="number" min="1"></label><button :disabled="!head || !rollbackId" @click="run(() => api(`/internal/knowledge/${release}/rollback`, 'POST', { snapshot_id: rollbackId, expected_head: head }))">回滚发布指针</button></div></section>
  <JsonResult :value="output" :error="error" :busy="busy" />
</template>
