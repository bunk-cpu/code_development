export async function api<T = unknown>(path: string, method = "GET", body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`)
  return data as T
}

export function display(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2)
}
