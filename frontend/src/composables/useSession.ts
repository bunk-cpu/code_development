import { onMounted, ref } from "vue"
import { api } from "../api/http"
import type { User } from "../types"

export function useSession(audience: "internal" | "customer") {
  const user = ref<User | null>(null)
  async function restore() {
    try {
      const result = await api<User>("/session")
      user.value = (audience === "customer") === (result.role === "customer") ? result : null
    } catch { user.value = null }
  }
  async function login(name: string, password: string) {
    const result = await api<User>("/session", "POST", { actor: name, password })
    user.value = { ...result, name: result.actor ?? name }
  }
  async function logout() { await api("/session", "DELETE"); user.value = null }
  onMounted(restore)
  return { user, login, logout }
}
