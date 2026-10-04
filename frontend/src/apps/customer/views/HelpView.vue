<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { api } from "../../../api/http"
import { useAction } from "../../../composables/useAction"
import type { Help } from "../../../types"
const { error, busy, run } = useAction()
const help = ref<Help | null>(null), query = ref(""), selectedPage = ref(new URLSearchParams(location.search).get("page"))
const previousRelease = new URLSearchParams(location.search).get("release")
const pages = computed(() => help.value?.pages.filter(p => (!selectedPage.value || p.page_id === selectedPage.value) && (p.title + p.content).includes(query.value)) ?? [])
async function load() { help.value = await api<Help>("/customer/help") }
onMounted(() => run(load))
</script>
<template><section><h2>产品帮助</h2><div class="row"><label>搜索手册 <input v-model="query" type="search" placeholder="输入功能名称"></label><button :disabled="busy" @click="run(load)">刷新帮助</button><span v-if="help" class="badge">适用版本 {{ help.release }}</span></div><button v-if="selectedPage" @click="selectedPage = null">查看全部帮助</button><p v-if="help && previousRelease && previousRelease !== help.release" class="notice">您的适用版本已更新，本页展示当前版本的已批准帮助。</p><p v-if="error" role="alert" class="notice">{{ error }}</p><p v-if="busy" role="status">正在读取帮助…</p><article v-for="page in pages" :key="page.page_id"><h3>{{ page.title }}</h3><div class="markdown-content" v-html="page.content_html"></div></article><p v-if="!busy && !pages.length">暂未找到相关帮助，请使用问答助手或联系支持人员。</p></section></template>
