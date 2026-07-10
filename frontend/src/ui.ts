export function scoreColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return 'text-slate-400'
  if (score >= 0.85) return 'text-emerald-400'
  if (score >= 0.6) return 'text-amber-400'
  return 'text-red-400'
}

export function scoreBg(score: number | null | undefined): string {
  if (score === null || score === undefined) return 'bg-slate-700'
  if (score >= 0.85) return 'bg-emerald-900/40 border-emerald-700'
  if (score >= 0.6) return 'bg-amber-900/40 border-amber-700'
  return 'bg-red-900/40 border-red-700'
}

export function statusColor(status: string | null | undefined): string {
  switch (status) {
    case 'DEPLOYED': return 'text-emerald-400'
    case 'FAILED': return 'text-red-400'
    case 'ROLLED_BACK': return 'text-amber-400'
    case 'TESTING': return 'text-sky-400'
    default: return 'text-slate-400'
  }
}

export function coverageStatusColor(status: string | null | undefined): string {
  switch (status) {
    case 'COVERED': return 'text-emerald-400'
    case 'PARTIAL': return 'text-amber-400'
    case 'GAP': return 'text-red-400'
    default: return 'text-slate-400'
  }
}

export function severityColor(severity: string | null | undefined): string {
  switch (severity) {
    case 'CRITICAL': return 'text-red-400'
    case 'HIGH': return 'text-orange-400'
    case 'LOW': return 'text-amber-400'
    case 'NONE': return 'text-emerald-400'
    default: return 'text-slate-400'
  }
}
