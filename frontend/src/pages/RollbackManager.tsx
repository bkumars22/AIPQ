import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type RollbackVersion } from '../api/client'
import { scoreColor, statusColor } from '../ui'

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—')
const demo = import.meta.env.VITE_DEMO_MODE === 'true'

function RollbackForm({ promptId, from, to, onDone, onCancel }: {
  promptId: number
  from: number
  to: RollbackVersion
  onDone: (message: string) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState('')
  const [name, setName] = useState('')
  const queryClient = useQueryClient()
  const roll = useMutation({
    mutationFn: () => api.rollBackPrompt(promptId, to.version_number, reason.trim(), name.trim() || undefined),
    onSuccess: r => {
      queryClient.invalidateQueries({ queryKey: ['rollbacks', promptId] })
      queryClient.invalidateQueries({ queryKey: ['versions', promptId] })
      onDone(`Rolled back from v${r.from_version_number} to v${r.to_version_number}. Recorded as rollback #${r.rollback_id}.`)
    },
  })
  const tooShort = reason.trim().length < 3

  return (
    <div className="rounded border border-amber-700 bg-amber-950/30 p-4 space-y-3" role="dialog" aria-label="Confirm rollback">
      <p className="text-sm text-amber-200">
        Roll back from <b>v{from}</b> to <b>v{to.version_number}</b>
        {to.quality_score !== null && <> (it scored {to.quality_score.toFixed(2)} when it passed the quality gate)</>}?
        This changes which version is live. It is recorded with your reason, and v{from} is marked ROLLED_BACK.
      </p>
      <label className="block text-xs text-slate-400">
        Reason <span className="text-red-400">(required)</span>
        <textarea
          value={reason}
          onChange={e => setReason(e.target.value)}
          rows={2}
          maxLength={1000}
          placeholder="Why is this version being restored?"
          className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
        />
      </label>
      <label className="block text-xs text-slate-400">
        Your name <span className="text-slate-600">(optional; your login is always recorded as well)</span>
        <input
          type="text"
          value={name}
          onChange={e => setName(e.target.value)}
          maxLength={120}
          className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
        />
      </label>
      {roll.isError && <p className="text-red-400 text-sm" role="alert">{(roll.error as Error).message}</p>}
      <div className="flex gap-2">
        <button
          onClick={() => roll.mutate()}
          disabled={tooShort || roll.isPending}
          className="px-3 py-1.5 rounded-md bg-amber-600 hover:bg-amber-500 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium"
        >
          {roll.isPending ? 'Rolling back…' : `Roll back to v${to.version_number}`}
        </button>
        <button onClick={onCancel} disabled={roll.isPending} className="px-3 py-1.5 rounded-md border border-slate-700 text-sm text-slate-300 hover:bg-slate-800">
          Cancel
        </button>
      </div>
    </div>
  )
}

export default function RollbackManager() {
  const { promptId } = useParams()
  const id = Number(promptId)
  const navigate = useNavigate()
  const [target, setTarget] = useState<RollbackVersion | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['rollbacks', id],
    queryFn: () => api.rollbackHistory(id),
  })

  return (
    <div className="max-w-5xl mx-auto px-8 py-6 space-y-5">
      <button onClick={() => navigate(-1)} className="text-slate-400 hover:text-slate-200 text-sm">&larr; Back</button>
      <h1 className="text-xl font-semibold">
        Version history and rollbacks{data ? <> &mdash; <span className="text-sky-300">{data.prompt_name}</span></> : null}
      </h1>

      {isLoading && <p className="text-slate-400">Loading…</p>}
      {error && <p className="text-red-400">Could not load rollback history: {(error as Error).message}</p>}

      {data && (
        <>
          {!data.targeted_rollback_enabled && (
            <div className="rounded border border-amber-800 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
              {demo
                ? 'This is the demo: the history below is read-only and no rollback can be performed.'
                : 'Rolling back to a chosen version is switched off on this deployment (QCP_ENABLED is not set). The history below is read-only.'}
            </div>
          )}
          {done && <div className="rounded border border-emerald-700 bg-emerald-950/30 px-3 py-2 text-sm text-emerald-200" role="status">{done}</div>}

          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-300">Versions</h2>
            <p className="text-xs text-slate-500">
              Only a version that previously passed the quality gate can be restored. A failed or still-testing version cannot.
            </p>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 text-left border-b border-slate-700">
                  <th className="pb-1 pr-4">Version</th>
                  <th className="pb-1 pr-4">Status</th>
                  <th className="pb-1 pr-4">Score</th>
                  <th className="pb-1 pr-4">Message</th>
                  <th className="pb-1 pr-4">Deployed</th>
                  <th className="pb-1">Rollback</th>
                </tr>
              </thead>
              <tbody>
                {data.versions.map(v => (
                  <tr key={v.version_number} className="border-b border-slate-800 align-top">
                    <td className="py-1 pr-4">v{v.version_number}{v.is_current && <span className="ml-2 text-xs text-emerald-400">live</span>}</td>
                    <td className={`py-1 pr-4 ${statusColor(v.status)}`}>{v.status}</td>
                    <td className={`py-1 pr-4 ${scoreColor(v.quality_score)}`}>{v.quality_score !== null ? v.quality_score.toFixed(2) : '—'}</td>
                    <td className="py-1 pr-4 text-slate-400">{v.change_message ?? '—'}</td>
                    <td className="py-1 pr-4 text-slate-400">{fmt(v.deployed_at)}</td>
                    <td className="py-1">
                      {v.can_roll_back_to ? (
                        <button
                          onClick={() => { setTarget(v); setDone(null) }}
                          disabled={!data.targeted_rollback_enabled}
                          title={data.targeted_rollback_enabled ? undefined : 'Rollback is switched off on this deployment'}
                          className="text-xs text-amber-300 hover:text-amber-200 disabled:text-slate-600 disabled:cursor-not-allowed"
                        >
                          Roll back to v{v.version_number}
                        </button>
                      ) : (
                        <span className="text-xs text-slate-600">{v.why_not}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {target && data.current_version_number !== null && (
            <RollbackForm
              key={target.version_number}
              promptId={id}
              from={data.current_version_number}
              to={target}
              onDone={message => { setDone(message); setTarget(null) }}
              onCancel={() => setTarget(null)}
            />
          )}

          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-300">Rollback history</h2>
            {data.rollbacks.length === 0 ? (
              <p className="text-sm text-slate-500">No rollbacks have been recorded for this prompt.</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-slate-400 text-left border-b border-slate-700">
                    <th className="pb-1 pr-4">When</th>
                    <th className="pb-1 pr-4">Change</th>
                    <th className="pb-1 pr-4">Type</th>
                    <th className="pb-1 pr-4">Requested by</th>
                    <th className="pb-1">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rollbacks.map(r => (
                    <tr key={r.id} className="border-b border-slate-800 align-top">
                      <td className="py-1 pr-4 text-slate-400">{fmt(r.triggered_at)}</td>
                      <td className="py-1 pr-4">v{r.from_version_number} &rarr; v{r.to_version_number}</td>
                      <td className={`py-1 pr-4 text-xs font-semibold ${r.triggered_by === 'AUTOMATIC' ? 'text-sky-400' : 'text-amber-300'}`}>{r.triggered_by}</td>
                      <td className="py-1 pr-4 text-slate-300">{r.requested_by ?? <span className="text-slate-600">automatic (drift)</span>}</td>
                      <td className="py-1 text-slate-400">{r.reason ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      <div>
        <Link to={`/golden-cases/${id}`} className="text-xs text-sky-400 hover:text-sky-300">Manage golden dataset &rarr;</Link>
      </div>
    </div>
  )
}
