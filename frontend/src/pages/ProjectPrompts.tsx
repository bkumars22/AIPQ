import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type CompletenessReport, type VersionConfidence } from '../api/client'
import { completenessStatusColor, completenessStatusDot, scoreColor, statusColor, severityColor } from '../ui'

const LAYER_LABELS: Record<string, string> = {
  llm_quality: 'LLM Quality', rag_quality: 'RAG Quality', behavioral: 'Behavioral',
  drift: 'Drift', production: 'Production',
}

function CompleteValidationPanel({ promptId }: { promptId: number }) {
  const [report, setReport] = useState<CompletenessReport | null>(null)

  const runValidation = useMutation({
    mutationFn: () => api.validateComplete(promptId),
    onSuccess: (result) => setReport(result),
  })

  return (
    <div className="text-sm rounded border border-slate-700 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="font-semibold text-slate-200">Complete Validation</div>
        <button
          onClick={() => runValidation.mutate()}
          disabled={runValidation.isPending}
          className="px-3 py-1 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-40 disabled:cursor-not-allowed text-xs font-medium"
        >
          {runValidation.isPending ? 'Running all 5 layers…' : 'Run Complete Validation'}
        </button>
      </div>

      {runValidation.isError && (
        <p className="text-red-400 text-xs">{(runValidation.error as Error).message}</p>
      )}

      {report && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-2">
            <span className="text-slate-400">Overall completeness:</span>
            <span className={`text-lg font-semibold ${scoreColor(report.overall_score !== null ? report.overall_score / 100 : null)}`}>
              {report.overall_score !== null ? `${report.overall_score.toFixed(0)}/100` : 'N/A'}
            </span>
          </div>

          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 text-left">
                <th className="pr-4 pb-1">Layer</th>
                <th className="pr-4 pb-1">Status</th>
                <th className="pr-4 pb-1">Score</th>
                <th className="pb-1">Detail</th>
              </tr>
            </thead>
            <tbody>
              {report.layers.map(l => (
                <tr key={l.name} className={`border-t border-slate-800 ${l.name === report.weakest_layer ? 'bg-slate-800/60' : ''}`}>
                  <td className="pr-4 py-1.5 text-slate-300">{LAYER_LABELS[l.name] ?? l.name}</td>
                  <td className="pr-4 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={`inline-block w-2 h-2 rounded-full ${completenessStatusDot(l.status)}`} />
                      <span className={completenessStatusColor(l.status)}>{l.status}</span>
                    </span>
                  </td>
                  <td className="pr-4 py-1.5 text-slate-400">{l.score !== null ? l.score.toFixed(0) : '—'}</td>
                  <td className="py-1.5 text-slate-500">{l.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {report.weakest_layer && (
            <div className="text-amber-400 text-xs rounded bg-amber-900/20 border border-amber-800 p-2">
              {report.recommendation}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ConfidenceCell({ vc }: { vc: VersionConfidence | undefined }) {
  if (!vc || vc.mean_score === null) return <span className="text-slate-500">—</span>

  const ci = vc.confidence_interval_95
  const margin = ci ? (ci[1] - ci[0]) / 2 : 0
  const vp = vc.vs_previous

  let vsPreviousLabel: string | null = null
  let vsPreviousColor = 'text-slate-500'
  if (vp) {
    if (vp.p_value === null) {
      vsPreviousLabel = `vs v${vp.version_number}: ${vp.recommendation}`
    } else if (vp.is_significant) {
      const better = (vp.effect_size ?? 0) > 0
      vsPreviousLabel = `${better ? 'Significantly better' : 'Significantly worse'} than v${vp.version_number} (p=${vp.p_value.toFixed(4)}, ${vp.effect_size_label} effect)`
      vsPreviousColor = better ? 'text-emerald-400' : 'text-red-400'
    } else {
      vsPreviousLabel = `No significant difference vs v${vp.version_number} (p=${vp.p_value.toFixed(4)})`
    }
  }

  return (
    <div>
      <div className="text-slate-300">
        {vc.mean_score.toFixed(2)}{margin > 0 && <span className="text-slate-500"> ± {margin.toFixed(2)}</span>}
        <span className="text-xs text-slate-500"> (95% CI, n={vc.sample_size})</span>
      </div>
      {vsPreviousLabel && <div className={`text-xs mt-0.5 ${vsPreviousColor}`}>{vsPreviousLabel}</div>}
    </div>
  )
}

export default function ProjectPrompts() {
  const { projectId } = useParams<{ projectId: string }>()
  const id = Number(projectId)
  const [expanded, setExpanded] = useState<number | null>(null)

  const { data: prompts, isLoading, error } = useQuery({
    queryKey: ['prompts', id],
    queryFn: () => api.listPrompts(id),
    refetchInterval: 15000,
  })

  return (
    <div className="max-w-5xl mx-auto p-8">
      <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">&larr; All projects</Link>
      <h1 className="text-3xl font-bold mt-2 mb-6">Prompts</h1>

      {isLoading && <p className="text-slate-400">Loading…</p>}
      {error && <p className="text-red-400">Failed to load: {(error as Error).message}</p>}

      <div className="space-y-3">
        {prompts?.map(p => (
          <div key={p.id} className="rounded-lg border border-slate-700 bg-slate-800/50">
            <button
              className="w-full flex items-center justify-between p-4 text-left"
              onClick={() => setExpanded(expanded === p.id ? null : p.id)}
            >
              <div>
                <div className="font-mono text-sm">{p.prompt_name}</div>
                {p.description && <div className="text-xs text-slate-400 mt-0.5">{p.description}</div>}
              </div>
              <div className="flex items-center gap-5 text-sm">
                <span className="text-slate-400">
                  v{p.current_version_number ?? '—'}
                </span>
                <span className={statusColor(p.status)}>{p.status ?? 'NO VERSION'}</span>
                <span className={`font-semibold ${scoreColor(p.quality_score)}`}>
                  {p.quality_score !== null ? p.quality_score.toFixed(2) : '—'}
                </span>
              </div>
            </button>
            {expanded === p.id && (
              <PromptDetail promptId={p.id} projectId={id} promptName={p.prompt_name} />
            )}
          </div>
        ))}
      </div>

      {prompts && prompts.length === 0 && (
        <p className="text-slate-400">No prompts registered for this project yet.</p>
      )}
    </div>
  )
}

function PromptDetail({ promptId, projectId, promptName }: { promptId: number; projectId: number; promptName: string }) {
  const navigate = useNavigate()
  const [selected, setSelected] = useState<number[]>([])
  const [abError, setAbError] = useState<string | null>(null)

  const { data: versions } = useQuery({
    queryKey: ['versions', promptId],
    queryFn: () => api.listVersions(promptId),
  })
  const { data: drift } = useQuery({
    queryKey: ['drift', projectId, promptName],
    queryFn: () => api.driftStatus(projectId, promptName),
  })
  const { data: confidence } = useQuery({
    queryKey: ['confidence', promptId],
    queryFn: () => api.promptConfidence(promptId),
  })
  const confidenceByVersion = new Map((confidence?.versions ?? []).map(v => [v.version_id, v]))
  const { data: causalImpact } = useQuery({
    queryKey: ['causal-impact', promptId],
    queryFn: () => api.causalImpact(promptId),
  })
  const { data: causalAttribution } = useQuery({
    queryKey: ['causal-attribution', promptId],
    queryFn: () => api.causalAttribution(promptId),
    staleTime: 10 * 60 * 1000, // makes real LLM calls per changed factor — don't refetch aggressively
  })
  const { data: portability } = useQuery({
    queryKey: ['portability', promptId],
    queryFn: () => api.portability(promptId),
    staleTime: 10 * 60 * 1000, // one real LLM call per configured provider — don't refetch aggressively
  })

  const startTest = useMutation({
    mutationFn: () => api.createABTest(promptId, selected[0], selected[1]),
    onSuccess: (result) => navigate(`/ab-tests/${result.ab_test_id}`),
    onError: (err: Error) => setAbError(err.message),
  })

  function toggleSelect(versionId: number) {
    setAbError(null)
    setSelected(prev => {
      if (prev.includes(versionId)) return prev.filter(id => id !== versionId)
      if (prev.length === 2) return [prev[1], versionId]
      return [...prev, versionId]
    })
  }

  return (
    <div className="border-t border-slate-700 p-4 space-y-4">
      {drift && (
        <div className="text-sm">
          <span className={`font-semibold ${severityColor(drift.recent_drift_severity)}`}>
            {drift.recent_drift_severity ?? 'NONE'}
          </span>
          <span className="text-slate-400"> — {drift.root_cause_hint}</span>
        </div>
      )}

      {causalImpact && causalImpact.estimated_effect !== null && (
        <div className="text-sm" title={causalImpact.caveat}>
          <span className={causalImpact.is_significant ? (causalImpact.estimated_effect < 0 ? 'text-red-400 font-semibold' : 'text-emerald-400 font-semibold') : 'text-slate-400'}>
            Causal impact of current version: {causalImpact.estimated_effect > 0 ? '+' : ''}{causalImpact.estimated_effect.toFixed(4)}
            {causalImpact.relative_effect_pct !== null && ` (${causalImpact.relative_effect_pct > 0 ? '+' : ''}${causalImpact.relative_effect_pct}%)`}
          </span>
          <span className="text-slate-500"> — {causalImpact.interpretation}</span>
        </div>
      )}

      {causalAttribution && causalAttribution.factors.some(f => f.changed) && (
        <div className="text-sm rounded border border-slate-700 p-3">
          <div className="text-slate-300 mb-2">{causalAttribution.interpretation}</div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500 text-left">
                <th className="pr-4 pb-1">Factor</th>
                <th className="pr-4 pb-1">Current</th>
                <th className="pr-4 pb-1">Previous</th>
                <th className="pr-4 pb-1">Counterfactual score</th>
                <th className="pb-1">Share of gap</th>
              </tr>
            </thead>
            <tbody>
              {causalAttribution.factors.filter(f => f.changed).map(f => (
                <tr key={f.factor} className="border-t border-slate-800" title={f.note}>
                  <td className="pr-4 py-1 text-slate-300">{f.factor.replace(/_/g, ' ')}</td>
                  <td className="pr-4 py-1 text-slate-400">{String(f.current_value)}</td>
                  <td className="pr-4 py-1 text-slate-400">{String(f.previous_value)}</td>
                  <td className="pr-4 py-1 text-slate-400">{f.counterfactual_score?.toFixed(4) ?? '—'}</td>
                  <td className="py-1 font-semibold text-slate-300">{f.share_pct !== null ? `${f.share_pct}%` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {portability && portability.scores.length > 0 && (
        <div className="text-sm rounded border border-slate-700 p-3">
          <div className={portability.warning ? 'text-amber-400 mb-2' : 'text-slate-300 mb-2'}>
            {portability.interpretation}
          </div>
          <div className="flex gap-4 text-xs">
            {portability.scores.map(s => (
              <div key={s.provider}>
                <span className="text-slate-500">{s.provider}: </span>
                <span className={s.overall_score === portability.max_score ? 'text-emerald-400 font-semibold' : 'text-slate-300'}>
                  {s.overall_score !== null ? s.overall_score.toFixed(2) : `error (${s.error})`}
                </span>
              </div>
            ))}
          </div>
          {portability.providers_skipped.length > 0 && (
            <div className="text-xs text-slate-600 mt-1">
              Not tested (no API key configured): {portability.providers_skipped.join(', ')}
            </div>
          )}
        </div>
      )}

      <CompleteValidationPanel promptId={promptId} />

      <table className="w-full text-sm">
        <thead>
          <tr className="text-slate-400 text-left border-b border-slate-700">
            <th className="pb-1 pr-4">A/B</th>
            <th className="pb-1 pr-4">Version</th>
            <th className="pb-1 pr-4">Status</th>
            <th className="pb-1 pr-4">Score</th>
            <th className="pb-1 pr-4">Confidence (live)</th>
            <th className="pb-1 pr-4">Changed by</th>
            <th className="pb-1 pr-4">Message</th>
            <th className="pb-1">Deployed</th>
          </tr>
        </thead>
        <tbody>
          {versions?.map(v => (
            <tr key={v.id} className="border-b border-slate-800">
              <td className="py-1 pr-4">
                <input
                  type="checkbox"
                  checked={selected.includes(v.id)}
                  onChange={() => toggleSelect(v.id)}
                  aria-label={`Select v${v.version_number} for A/B test`}
                />
              </td>
              <td className="py-1 pr-4">v{v.version_number}</td>
              <td className={`py-1 pr-4 ${statusColor(v.status)}`}>{v.status}</td>
              <td className={`py-1 pr-4 ${scoreColor(v.quality_score)}`}>
                {v.quality_score !== null ? v.quality_score.toFixed(2) : '—'}
              </td>
              <td className="py-1 pr-4">
                <ConfidenceCell vc={confidenceByVersion.get(v.id)} />
              </td>
              <td className="py-1 pr-4">{v.changed_by}</td>
              <td className="py-1 pr-4 text-slate-400">{v.change_message ?? '—'}</td>
              <td className="py-1 text-slate-400">
                {v.deployed_at ? new Date(v.deployed_at).toLocaleString() : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="flex items-center gap-3">
        <button
          onClick={() => startTest.mutate()}
          disabled={selected.length !== 2 || startTest.isPending}
          className="px-3 py-1.5 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium"
        >
          Start A/B Test
        </button>
        <span className="text-xs text-slate-500">
          {selected.length === 2 ? 'Ready to compare' : 'Select two versions to compare'}
        </span>
      </div>
      {abError && <p className="text-red-400 text-sm">{abError}</p>}
    </div>
  )
}
