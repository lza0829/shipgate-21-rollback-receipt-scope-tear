import React, { useEffect, useState } from 'react'
import { api } from '../api'

const SCOPES = ['全部', '回滚', '回退拒因', '回退冲突', '回滚窗', '晋升', '发版单', '探针', '设置', '放行窗', '系统']

export default function Audit() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')
  const [scope, setScope] = useState('全部')
  const [kw, setKw] = useState('')
  const [refId, setRefId] = useState('')

  const load = () => {
    const params = new URLSearchParams()
    if (scope !== '全部') params.set('类别', scope)
    if (kw.trim()) params.set('动作关键字', kw.trim())
    if (refId.trim()) params.set('关联编号', refId.trim())
    const qs = params.toString()
    return api(`/审计${qs ? `?${qs}` : ''}`).then(setRows).catch((e) => setErr(e.message))
  }

  useEffect(() => { load() }, [scope])

  return (
    <div className="panel">
      <h2>审计流水（范围过滤）</h2>
      {err && <p className="err">{err}</p>}
      <div className="row">
        <label>类别
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            {SCOPES.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <label>动作关键字<input value={kw} onChange={(e) => setKw(e.target.value)} placeholder="如 时段外" /></label>
        <label>关联编号<input value={refId} onChange={(e) => setRefId(e.target.value)} placeholder="发版单编号" /></label>
        <button type="button" onClick={load}>查询</button>
        <button type="button" className="secondary" onClick={() => { setScope('全部'); setKw(''); setRefId(''); }}>重置</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>类别</th><th>动作</th><th>关联</th><th>备注</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号}>
                <td>{r.时间}</td>
                <td style={{ color: r.类别 === '回退拒因' ? 'var(--danger)' : undefined }}>{r.类别}</td>
                <td>{r.动作}</td><td>{r.关联编号 || '-'}</td><td>{r.备注}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
