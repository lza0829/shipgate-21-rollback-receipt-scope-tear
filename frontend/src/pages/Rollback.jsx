import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Rollback() {
  const [rows, setRows] = useState([])
  const [wins, setWins] = useState([])
  const [rid, setRid] = useState('')
  const [ver, setVer] = useState('1.0.0')
  const [err, setErr] = useState('')
  const [receipt, setReceipt] = useState(null)

  const load = async () => {
    const [all, rb] = await Promise.all([api('/发版单'), api('/回滚窗')])
    const active = all.filter((r) => ['已放行', '晋升中'].includes(r.状态))
    setRows(active)
    setWins(rb)
    if (!rid && active[0]) setRid(String(active[0].编号))
  }

  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])

  const selected = rows.find((r) => String(r.编号) === rid)
  const win = selected && wins.find((w) => w.环境 === selected.当前环境)

  const go = async () => {
    setErr(''); setReceipt(null)
    try {
      const rc = await api(`/发版单/${rid}/回滚`, {
        method: 'POST', body: JSON.stringify({ 目标版本: ver }),
      })
      setReceipt(rc)
      await load()
    } catch (e) {
      setErr(e.message)
      // 后端硬拒时 detail 内含整笔回执（时段、单、名册、实例、对账、审计）
      if (e.receipt) setReceipt(e.receipt)
    }
  }

  return (
    <div className="panel">
      <h2>回滚（回退时段硬闸）</h2>
      <p className="muted">
        时段关闭时写口整笔拒绝并留审计痕迹，不依赖按钮置灰；读侧「可回退」与写侧卡闸同一判定。
      </p>
      {err && <p className="err">{err}</p>}
      <div className="row">
        <label>发版单
          <select value={rid} onChange={(e) => { setRid(e.target.value); setReceipt(null) }}>
            {rows.map((r) => <option key={r.编号} value={r.编号}>{r.发版单号} · {r.应用} · {r.当前环境}</option>)}
          </select>
        </label>
        <label>回滚目标版本<input value={ver} onChange={(e) => setVer(e.target.value)} /></label>
        {/* 按钮始终可点：真正的闸在写侧，点击后由后端给成败与回执，而非只灰按钮 */}
        <button type="button" className="danger" onClick={go}>执行回滚</button>
        <button type="button" className="secondary" onClick={() => load().catch((e) => setErr(e.message))}>刷新</button>
      </div>

      {win && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>环境</th><th>窗口状态</th><th>可回退</th><th>开启</th><th>关闭</th><th>开闭戳</th></tr></thead>
            <tbody>
              <tr>
                <td>{win.环境}</td>
                <td>{win.状态}</td>
                <td style={{ color: win.可回退 ? 'var(--ok)' : 'var(--danger)', fontWeight: 700 }}>
                  {win.可回退 ? '是（时段内可写）' : '否（写口将整笔拒绝）'}
                </td>
                <td>{win.开启}</td><td>{win.关闭}</td><td>{win.开闭戳 || '-'}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {receipt && <ReceiptCard rc={receipt} />}
    </div>
  )
}

function ReceiptCard({ rc }) {
  const failed = rc.成败 === '失败'
  return (
    <div className="panel" style={{ borderTopColor: failed ? 'var(--danger)' : 'var(--ok)', marginTop: '1rem' }}>
      <h2>回退回执 · {rc.成败}{failed && rc.拒因 ? `（${rc.拒因}）` : ''}</h2>
      <div className="table-wrap">
        <table>
          <tbody>
            <tr><th>回退时段</th>
              <td>{rc.回退时段?.环境} / {rc.回退时段?.状态} / 可回退={String(rc.回退时段?.可回退)} / 开闭戳={rc.回退时段?.开闭戳 || '-'}</td></tr>
            <tr><th>发版单</th>
              <td>状态={rc.发版单?.状态} · 目标版本={rc.发版单?.目标版本} · 回滚时间={rc.发版单?.回滚时间 || '-'}</td></tr>
            <tr><th>名册目标版本</th><td>{rc.名册目标版本}</td></tr>
            <tr><th>实例</th>
              <td>{(rc.实例 || []).map((i, k) => `${i.版本}/${i.健康}`).join('，') || '-'}</td></tr>
            <tr><th>对账桶</th>
              <td>窗口内成功={rc.对账桶?.窗口内成功回退} · 时段外尝试={rc.对账桶?.时段外回退尝试} · 时段外成功={rc.对账桶?.时段外成功回退}</td></tr>
            <tr><th>审计</th>
              <td>{rc.审计 ? `${rc.审计.kind} · ${rc.审计.action} · ${rc.审计.created_at}` : '-'}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}
