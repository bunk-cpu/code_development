<script setup lang="ts">
import { display } from "../api/http"
defineProps<{ value?: unknown; error?: string; busy?: boolean }>()
</script>
<template>
  <p v-if="busy" role="status" class="muted">处理中…</p>
  <p v-if="error" role="alert" class="notice">{{ error }}</p>
  <details v-if="value != null" open class="result"><summary>结果与证据</summary><pre>{{ display(value) }}</pre></details>
</template>
