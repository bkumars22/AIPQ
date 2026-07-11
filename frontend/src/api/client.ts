import { DEMO_AB_TEST_RESULTS, DEMO_BUSINESS_METRICS, DEMO_CAUSAL_ATTRIBUTION, DEMO_CAUSAL_IMPACT, DEMO_CONFIDENCE, DEMO_DRIFT, DEMO_PROJECTS, DEMO_PROMPTS, DEMO_VERSIONS } from './demoData'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001'
const DEV_JWT = import.meta.env.VITE_DEV_JWT || ''
const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === 'true'

// Demo builds (GitHub Pages) have no live backend — a tiny artificial delay
// keeps the loading state from being an imperceptible flash, same as a real request.
const demoDelay = <T,>(value: T): Promise<T> => new Promise(resolve => setTimeout(() => resolve(value), 200))

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${DEV_JWT}` },
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} — ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${DEV_JWT}`, 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} — ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

export interface ProjectSummary {
  id: number
  name: string
  pipeline_type: string
  prompt_count: number
  avg_quality_score: number | null
  created_at: string
}

export interface PromptSummary {
  id: number
  prompt_name: string
  description: string | null
  current_version_number: number | null
  quality_score: number | null
  status: string | null
  deployed_at: string | null
}

export interface PromptVersionSummary {
  id: number
  version_number: number
  quality_score: number | null
  status: string
  changed_by: string
  change_message: string | null
  created_at: string
  deployed_at: string | null
}

export interface DriftStatus {
  prompt_id: number
  prompt_name: string
  current_version_id: number | null
  current_version_number: number | null
  deployed_at: string | null
  quality_score: number | null
  recent_drift_severity: string | null
  changed_recently: boolean
  root_cause_hint: string
}

export interface BusinessMetrics {
  time_saved: {
    iterations_this_month: number
    manual_minutes: number
    automated_minutes: number
    saved_minutes: number
    saved_pct: number
    assumptions: { manual_minutes_per_iteration: number; aipq_minutes_per_iteration: number }
  }
  incidents_prevented: {
    blocked_deployments: number
    avg_degradation_prevented: number
    estimated_impact_prevented: number
    assumptions: { sessions_per_deployment: number }
  }
  rollback_speed: {
    manual_baseline_minutes: number
    aipq_avg_minutes: number | null
    improvement_pct: number | null
    automatic_rollback_count: number
  }
  quality_trend: Record<string, { date: string; avg_score: number }[]>
  coverage_gaps: {
    project_id: number; project_name: string; prompt_name: string; category: string
    score: number; status: string; recommendation: string
  }[]
  predictions: {
    project_id: number; project_name: string; prompt_name: string
    days_until_risk: number | null; risk_level: string; recommendation: string
    confidence_interval_7d?: {
      lower: number | null; upper: number | null; confidence_level: number
      calibration_size: number; guarantee: string
    }
  }[]
}

export interface ConfidenceVsPrevious {
  version_number: number
  p_value: number | null
  effect_size: number | null
  effect_size_label: string | null
  is_significant: boolean
  recommendation: string
}

export interface VersionConfidence {
  version_id: number
  version_number: number
  sample_size: number
  mean_score: number | null
  confidence_interval_95: [number, number] | null
  vs_previous: ConfidenceVsPrevious | null
}

export interface PromptConfidence {
  prompt_id: number
  versions: VersionConfidence[]
}

export interface CausalImpact {
  prompt_id: number
  pre_period_mean: number | null
  post_period_mean: number | null
  counterfactual_mean: number | null
  estimated_effect: number | null
  relative_effect_pct: number | null
  p_value: number | null
  is_significant: boolean
  sample_size_pre: number
  sample_size_post: number
  interpretation: string
  caveat: string
}

export interface CausalFactor {
  factor: string
  changed: boolean
  current_value: number
  previous_value: number
  counterfactual_score: number | null
  recovered_effect: number | null
  share_pct: number | null
  note: string
}

export interface CausalAttribution {
  prompt_id: number
  current_version_id: number | null
  previous_version_id: number | null
  current_score: number | null
  previous_score: number | null
  total_gap: number | null
  factors: CausalFactor[]
  interpretation: string
}

export interface ABTestArmStats {
  version_id: number
  version_number: number
  n: number
  mean_score: number | null
  stdev: number | null
}

export interface ABTestResults {
  ab_test_id: number
  prompt_id: number
  status: string  // RUNNING | COMPLETED | CANCELLED
  traffic_split: number
  min_samples: number
  current_samples: number
  version_a: ABTestArmStats
  version_b: ABTestArmStats
  p_value: number | null
  significant: boolean
  winner_version_id: number | null
  recommendation: string
}

export const api = {
  listProjects: () =>
    DEMO_MODE ? demoDelay(DEMO_PROJECTS)
      : apiGet<{ projects: ProjectSummary[] }>('/projects').then(r => r.projects),

  listPrompts: (projectId: number) =>
    DEMO_MODE ? demoDelay(DEMO_PROMPTS[projectId] ?? [])
      : apiGet<{ prompts: PromptSummary[] }>(`/projects/${projectId}/prompts`).then(r => r.prompts),

  listVersions: (promptId: number) =>
    DEMO_MODE ? demoDelay(DEMO_VERSIONS[promptId] ?? [])
      : apiGet<{ versions: PromptVersionSummary[] }>(`/prompts/${promptId}/versions`).then(r => r.versions),

  driftStatus: (projectId: number, promptName: string) =>
    DEMO_MODE ? demoDelay(DEMO_DRIFT[`${projectId}:${promptName}`])
      : apiGet<DriftStatus>(`/drift/status?project_id=${projectId}&prompt_name=${encodeURIComponent(promptName)}`),

  promptConfidence: (promptId: number) =>
    DEMO_MODE ? demoDelay(DEMO_CONFIDENCE[promptId] ?? { prompt_id: promptId, versions: [] })
      : apiGet<PromptConfidence>(`/prompts/${promptId}/confidence`),

  causalImpact: (promptId: number) =>
    DEMO_MODE ? demoDelay(DEMO_CAUSAL_IMPACT[promptId] ?? {
      prompt_id: promptId, pre_period_mean: null, post_period_mean: null, counterfactual_mean: null,
      estimated_effect: null, relative_effect_pct: null, p_value: null, is_significant: false,
      sample_size_pre: 0, sample_size_post: 0, interpretation: 'No previous version to compare against.',
      caveat: '',
    })
      : apiGet<CausalImpact>(`/prompts/${promptId}/causal-impact`),

  causalAttribution: (promptId: number) =>
    DEMO_MODE ? demoDelay(DEMO_CAUSAL_ATTRIBUTION[promptId] ?? {
      prompt_id: promptId, current_version_id: null, previous_version_id: null,
      current_score: null, previous_score: null, total_gap: null, factors: [],
      interpretation: 'No previous version to compare against.',
    })
      // Real LLM calls per changed factor — can take much longer than the
      // other analyze/* calls (backend proxy uses a 120s timeout for this
      // one specifically); apiGet's fetch() has no client-side timeout of
      // its own, so it just waits.
      : apiGet<CausalAttribution>(`/prompts/${promptId}/causal-attribution`),

  businessMetrics: () =>
    DEMO_MODE ? demoDelay(DEMO_BUSINESS_METRICS)
      : apiGet<BusinessMetrics>('/metrics/business'),

  createABTest: (promptId: number, versionAId: number, versionBId: number, minSamples = 100) =>
    DEMO_MODE ? demoDelay({ ab_test_id: DEMO_AB_TEST_RESULTS.ab_test_id, status: 'RUNNING' })
      : apiPost<{ ab_test_id: number; status: string }>('/ab-tests', {
          prompt_id: promptId, version_a_id: versionAId, version_b_id: versionBId, min_samples: minSamples,
        }),

  abTestResults: (id: number) =>
    DEMO_MODE ? demoDelay(DEMO_AB_TEST_RESULTS)
      : apiGet<ABTestResults>(`/ab-tests/${id}/results`),

  promoteABTest: (id: number, version: 'A' | 'B') =>
    DEMO_MODE ? demoDelay({ promoted_version_id: DEMO_AB_TEST_RESULTS.version_a.version_id, status: 'COMPLETED' })
      : apiPost<{ promoted_version_id: number; status: string }>(`/ab-tests/${id}/promote?version=${version}`),
}
