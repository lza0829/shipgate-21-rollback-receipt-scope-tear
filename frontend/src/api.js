export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { 明细: text } }
  if (!res.ok) {
    const msg = (data && (data.detail || data.明细)) || res.statusText
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    // 回退硬拒时 detail 形如 { 拒因, 回执 }，把整笔回执挂出供页面展示六处一致状态。
    if (data && data.detail && typeof data.detail === 'object' && data.detail.回执) {
      err.message = data.detail.拒因 || err.message
      err.receipt = data.detail.回执
    }
    throw err
  }
  return data
}
