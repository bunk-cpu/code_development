import { ref } from "vue"

export function useAction() {
  const output = ref<unknown>(null)
  const busy = ref(false)
  const error = ref("")
  async function run<T>(task: () => Promise<T>): Promise<T | undefined> {
    if (busy.value) return
    busy.value = true
    error.value = ""
    try { const result = await task(); output.value = result; return result }
    catch (e) { error.value = e instanceof Error ? e.message : String(e) }
    finally { busy.value = false }
  }
  return { output, busy, error, run }
}
