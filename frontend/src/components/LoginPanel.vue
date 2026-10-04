<script setup lang="ts">
import { ref } from "vue"
import { useAction } from "../composables/useAction"
import type { User } from "../types"
const props = defineProps<{ audience: "internal" | "customer"; user: User | null; login: (name: string, password: string) => Promise<void>; logout: () => Promise<void> }>()
const name = ref(props.audience === "internal" ? "analyst" : "customer-a")
const password = ref("demo-only")
const { run, error, busy } = useAction()
</script>
<template>
  <section class="session-panel">
    <form v-if="!user" class="row" @submit.prevent="run(() => login(name, password))">
      <label>账号 <select v-model="name"><template v-if="audience === 'internal'"><option>analyst</option><option>editor</option></template><template v-else><option value="customer-a">租户 A</option><option value="customer-b">租户 B</option></template></select></label>
      <label>口令 <input v-model="password" type="password" autocomplete="current-password" required></label>
      <button :disabled="busy" class="primary">登录</button>
    </form>
    <div v-else class="row"><span class="success">{{ user.name }} · {{ user.tenant ?? user.role }}</span><button :disabled="busy" @click="run(logout)">退出登录</button></div>
    <p v-if="error" role="alert" class="notice">{{ error }}</p>
  </section>
</template>
