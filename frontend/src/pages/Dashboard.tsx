import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { scoreColor, scoreBg } from '../ui'

export default function Dashboard() {
  const { data: projects, isLoading, error } = useQuery({
    queryKey: ['projects'],
    queryFn: api.listProjects,
    refetchInterval: 15000,
  })

  return (
    <div className="max-w-5xl mx-auto p-8">
      <h1 className="text-3xl font-bold mb-1">AIPQ</h1>
      <p className="text-slate-400 mb-8">AI Prompt Quality &amp; Drift Management</p>

      {isLoading && <p className="text-slate-400">Loading projects…</p>}
      {error && <p className="text-red-400">Failed to load: {(error as Error).message}</p>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {projects?.map(p => (
          <Link
            key={p.id}
            to={`/projects/${p.id}`}
            className={`block rounded-lg border p-5 hover:border-slate-500 transition-colors ${scoreBg(p.avg_quality_score)}`}
          >
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-xl font-semibold">{p.name}</h2>
              <span className="text-xs uppercase tracking-wide text-slate-400">{p.pipeline_type}</span>
            </div>
            <div className="flex items-center gap-6 text-sm">
              <div>
                <span className="text-slate-400">Prompts: </span>
                <span className="font-medium">{p.prompt_count}</span>
              </div>
              <div>
                <span className="text-slate-400">Avg quality: </span>
                <span className={`font-medium ${scoreColor(p.avg_quality_score)}`}>
                  {p.avg_quality_score !== null ? p.avg_quality_score.toFixed(2) : '—'}
                </span>
              </div>
            </div>
          </Link>
        ))}
      </div>

      {projects && projects.length === 0 && (
        <p className="text-slate-400">No projects registered yet.</p>
      )}
    </div>
  )
}
