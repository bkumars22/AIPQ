import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '../api/client'
import { coverageStatusColor, scoreColor, severityColor } from '../ui'

const PROJECT_COLORS: Record<string, string> = {
  ARIA: '#34d399', QAIP: '#38bdf8', SCIP: '#fbbf24', ZENTRAVIX: '#f472b6',
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3">{title}</h2>
      {children}
    </div>
  )
}

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div>
      <div className="text-2xl font-bold">{value}</div>
      <div className="text-xs text-slate-400 mt-0.5">{label}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
    </div>
  )
}

// Reshapes {ARIA: [{date, avg_score}], QAIP: [...]} into one array Recharts can plot
// as multiple lines: [{date, ARIA: score, QAIP: score}, ...]
function toChartRows(trend: Record<string, { date: string; avg_score: number }[]>) {
  const byDate = new Map<string, Record<string, number | string>>()
  for (const [project, points] of Object.entries(trend)) {
    for (const p of points) {
      const row = byDate.get(p.date) ?? { date: p.date }
      row[project] = p.avg_score
      byDate.set(p.date, row)
    }
  }
  return Array.from(byDate.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)))
}

export default function BusinessMetrics() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['business-metrics'],
    queryFn: api.businessMetrics,
    refetchInterval: 30000,
  })

  return (
    <div className="max-w-5xl mx-auto p-8">
      <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">&larr; All projects</Link>
      <h1 className="text-3xl font-bold mt-2 mb-1">Business Metrics</h1>
      <p className="text-slate-400 mb-6 text-sm">
        Figures marked with an asterisk (*) combine a real count from the database with a documented
        estimate — AIPQ doesn't track manual-iteration time or end-user session volume directly.
      </p>

      {isLoading && <p className="text-slate-400">Loading…</p>}
      {error && <p className="text-red-400">Failed to load: {(error as Error).message}</p>}

      {data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* 1. Time saved */}
          <Card title="Time Saved This Month *">
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Manual (est.)" value={`${Math.round(data.time_saved.manual_minutes / 60 * 10) / 10}h`} />
              <Stat label="AIPQ (est.)" value={`${Math.round(data.time_saved.automated_minutes / 60 * 10) / 10}h`} />
              <Stat
                label="Saved"
                value={<span className="text-emerald-400">{data.time_saved.saved_pct}%</span>}
                sub={`${data.time_saved.iterations_this_month} eval run(s) this month`}
              />
            </div>
          </Card>

          {/* 2. Incidents prevented */}
          <Card title="Incidents Prevented">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Blocked deployments" value={data.incidents_prevented.blocked_deployments} />
              <Stat
                label="Estimated impact prevented *"
                value={data.incidents_prevented.estimated_impact_prevented.toLocaleString()}
                sub={`avg degradation ${(data.incidents_prevented.avg_degradation_prevented * 100).toFixed(1)}%`}
              />
            </div>
          </Card>

          {/* 3. Rollback speed */}
          <Card title="Rollback Speed">
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Manual (baseline)" value={`${data.rollback_speed.manual_baseline_minutes}m`} />
              <Stat
                label="AIPQ (real)"
                value={data.rollback_speed.aipq_avg_minutes !== null ? `${data.rollback_speed.aipq_avg_minutes}m` : '—'}
                sub={`${data.rollback_speed.automatic_rollback_count} automatic rollback(s)`}
              />
              <Stat
                label="Improvement"
                value={
                  data.rollback_speed.improvement_pct !== null
                    ? <span className="text-emerald-400">{data.rollback_speed.improvement_pct}%</span>
                    : '—'
                }
              />
            </div>
          </Card>

          {/* 6. Prediction panel */}
          <Card title="Prediction Panel">
            {data.predictions.length === 0 && <p className="text-slate-500 text-sm">No deployed prompts to predict yet.</p>}
            <ul className="space-y-2">
              {data.predictions.map((p, i) => {
                const ci = p.confidence_interval_7d
                return (
                  <li key={i} className="text-sm">
                    <span className="font-mono">{p.project_name}/{p.prompt_name}</span>:{' '}
                    <span className={severityColor(p.risk_level === 'LOW' ? 'NONE' : p.risk_level)}>
                      {p.risk_level === 'LOW' ? 'stable' : `risk in ${p.days_until_risk} days ⚠️`}
                    </span>
                    {ci && ci.lower !== null && ci.upper !== null && (
                      <div className="text-xs text-slate-500 mt-0.5" title={ci.guarantee}>
                        {(ci.confidence_level * 100).toFixed(0)}% conformal interval (7d): {ci.lower.toFixed(2)}–{ci.upper.toFixed(2)}{' '}
                        (m={ci.calibration_size})
                      </div>
                    )}
                    {ci && ci.lower === null && (
                      <div className="text-xs text-slate-500 mt-0.5">{ci.guarantee}</div>
                    )}
                  </li>
                )
              })}
            </ul>
          </Card>

          {/* 4. Quality trend per project */}
          <Card title="Quality Trend Per Project — Last 30 Days">
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={toChartRows(data.quality_trend)}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                  <XAxis dataKey="date" stroke="#94a3b8" fontSize={12} />
                  <YAxis domain={[0, 1]} stroke="#94a3b8" fontSize={12} />
                  <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155' }} />
                  <Legend />
                  {Object.keys(data.quality_trend).map(project => (
                    <Line
                      key={project} type="monotone" dataKey={project}
                      stroke={PROJECT_COLORS[project] ?? '#94a3b8'} dot={false} connectNulls
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
            {Object.values(data.quality_trend).every(points => points.length === 0) && (
              <p className="text-slate-500 text-sm mt-2">No drift samples recorded in the last 30 days yet.</p>
            )}
          </Card>

          {/* 5. Coverage gaps summary */}
          <Card title="Coverage Gaps Summary">
            {data.coverage_gaps.length === 0 && <p className="text-slate-500 text-sm">No coverage gaps detected.</p>}
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 text-left border-b border-slate-700">
                  <th className="pb-1 pr-4">Prompt</th>
                  <th className="pb-1 pr-4">Category</th>
                  <th className="pb-1 pr-4">Coverage</th>
                  <th className="pb-1">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.coverage_gaps.map((g, i) => (
                  <tr key={i} className="border-b border-slate-800 align-top">
                    <td className="py-1 pr-4 font-mono text-xs">{g.project_name}/{g.prompt_name}</td>
                    <td className="py-1 pr-4">
                      {g.category.replace(/_/g, ' ')}
                      {g.recommendation && (
                        <div className="text-xs text-slate-500 mt-0.5 max-w-md">{g.recommendation}</div>
                      )}
                    </td>
                    <td className={`py-1 pr-4 font-semibold ${scoreColor(g.score)}`}>{Math.round(g.score * 100)}%</td>
                    <td className={`py-1 font-medium ${coverageStatusColor(g.status)}`}>{g.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      )}
    </div>
  )
}
