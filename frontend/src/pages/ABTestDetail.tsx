import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { scoreColor, statusColor } from '../ui'

function ArmCard({ label, version_number, n, mean_score, stdev, isWinner }: {
  label: string; version_number: number; n: number
  mean_score: number | null; stdev: number | null; isWinner: boolean
}) {
  return (
    <div className={`rounded-lg border p-5 ${isWinner ? 'border-emerald-600 bg-emerald-900/10' : 'border-slate-700 bg-slate-800/50'}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-400 uppercase tracking-wide">
          Version {label} — v{version_number}
        </h3>
        {isWinner && <span className="text-xs font-semibold text-emerald-400">WINNER</span>}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className={`text-2xl font-bold ${scoreColor(mean_score)}`}>
            {mean_score !== null ? mean_score.toFixed(3) : '—'}
          </div>
          <div className="text-xs text-slate-400 mt-0.5">Mean score</div>
        </div>
        <div>
          <div className="text-2xl font-bold text-slate-300">{n}</div>
          <div className="text-xs text-slate-400 mt-0.5">Samples</div>
        </div>
        <div>
          <div className="text-2xl font-bold text-slate-300">{stdev !== null ? stdev.toFixed(3) : '—'}</div>
          <div className="text-xs text-slate-400 mt-0.5">Std dev</div>
        </div>
      </div>
    </div>
  )
}

export default function ABTestDetail() {
  const { id } = useParams<{ id: string }>()
  const abTestId = Number(id)
  const queryClient = useQueryClient()
  const [promoteError, setPromoteError] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['ab-test', abTestId],
    queryFn: () => api.abTestResults(abTestId),
    refetchInterval: (query) => (query.state.data?.status === 'RUNNING' ? 5000 : false),
  })

  const promote = useMutation({
    mutationFn: (version: 'A' | 'B') => api.promoteABTest(abTestId, version),
    onSuccess: () => {
      setPromoteError(null)
      queryClient.invalidateQueries({ queryKey: ['ab-test', abTestId] })
    },
    onError: (err: Error) => setPromoteError(err.message),
  })

  return (
    <div className="max-w-5xl mx-auto p-8">
      <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">
        &larr; All projects
      </Link>
      <div className="flex items-center gap-3 mt-2 mb-6">
        <h1 className="text-3xl font-bold">A/B Test #{abTestId}</h1>
        {data && <span className={`text-sm font-semibold ${statusColor(data.status)}`}>{data.status}</span>}
      </div>

      {isLoading && <p className="text-slate-400">Loading…</p>}
      {error && <p className="text-red-400">Failed to load: {(error as Error).message}</p>}

      {data && (
        <div className="space-y-4">
          <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
            <div className="flex items-center justify-between text-sm mb-2">
              <span className="text-slate-400">Traffic split</span>
              <span className="text-slate-300">
                {Math.round(data.traffic_split * 100)}% A / {Math.round((1 - data.traffic_split) * 100)}% B
              </span>
            </div>
            <div className="h-2 rounded-full bg-slate-700 overflow-hidden flex">
              <div className="h-full bg-sky-500" style={{ width: `${data.traffic_split * 100}%` }} />
              <div className="h-full bg-fuchsia-500" style={{ width: `${(1 - data.traffic_split) * 100}%` }} />
            </div>
            <div className="text-xs text-slate-500 mt-2">
              {data.current_samples} / {data.min_samples} samples collected
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <ArmCard label="A" {...data.version_a} isWinner={data.winner_version_id === data.version_a.version_id} />
            <ArmCard label="B" {...data.version_b} isWinner={data.winner_version_id === data.version_b.version_id} />
          </div>

          <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3">
              Statistical Significance
            </h2>
            <div className="flex items-center gap-4 mb-3">
              <span className="text-2xl font-bold text-slate-300">
                p = {data.p_value !== null ? data.p_value.toFixed(4) : 'n/a'}
              </span>
              <span className={`text-sm font-semibold ${data.significant ? 'text-emerald-400' : 'text-slate-400'}`}>
                {data.significant ? 'SIGNIFICANT (p < 0.05)' : 'NOT YET SIGNIFICANT'}
              </span>
            </div>
            <p className="text-sm text-slate-400">{data.recommendation}</p>
          </div>

          {data.status === 'RUNNING' && (
            <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
              <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3">Promote Winner</h2>
              <div className="flex gap-3">
                <button
                  onClick={() => promote.mutate('A')}
                  disabled={promote.isPending}
                  className="px-4 py-2 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-sm font-medium"
                >
                  Promote Version A (v{data.version_a.version_number})
                </button>
                <button
                  onClick={() => promote.mutate('B')}
                  disabled={promote.isPending}
                  className="px-4 py-2 rounded-md bg-fuchsia-600 hover:bg-fuchsia-500 disabled:opacity-50 text-sm font-medium"
                >
                  Promote Version B (v{data.version_b.version_number})
                </button>
              </div>
              {promoteError && <p className="text-red-400 text-sm mt-2">{promoteError}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
