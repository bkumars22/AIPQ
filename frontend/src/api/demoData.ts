// Static demo data for the GitHub Pages build (no live backend there).
// This is not fabricated — it's the exact state produced during real,
// verified testing of the live stack: ARIA's prompt was deliberately
// drifted to prove the IsolationForest -> automatic rollback loop, and
// QAIP's prompt genuinely has no deployed version yet (its one evaluation
// attempt failed because this dev environment has no real GROQ_API_KEY).
import type { BusinessMetrics, DriftStatus, ProjectSummary, PromptSummary, PromptVersionSummary } from './client'

export const DEMO_PROJECTS: ProjectSummary[] = [
  {
    id: 1, name: 'ARIA', pipeline_type: 'LANGGRAPH',
    prompt_count: 1, avg_quality_score: 0.93,
    created_at: '2026-07-07T07:10:23.106699Z',
  },
  {
    id: 2, name: 'QAIP', pipeline_type: 'LANGGRAPH',
    prompt_count: 1, avg_quality_score: null,
    created_at: '2026-07-07T08:05:08.273766Z',
  },
]

export const DEMO_PROMPTS: Record<number, PromptSummary[]> = {
  1: [{
    id: 1, prompt_name: 'aria_socratic_system', description: null,
    current_version_number: 1, quality_score: 0.93, status: 'DEPLOYED',
    deployed_at: '2026-07-07T07:54:13.644109Z',
  }],
  2: [{
    id: 2, prompt_name: 'qaip_defect_explanation', description: null,
    current_version_number: null, quality_score: null, status: null,
    deployed_at: null,
  }],
}

export const DEMO_VERSIONS: Record<number, PromptVersionSummary[]> = {
  1: [
    {
      id: 2, version_number: 2, quality_score: 0.60, status: 'ROLLED_BACK',
      changed_by: 'kumar', change_message: 'sped up responses',
      created_at: '2026-07-07T07:39:12.423320Z', deployed_at: '2026-07-07T07:39:12.423320Z',
    },
    {
      id: 1, version_number: 1, quality_score: 0.93, status: 'DEPLOYED',
      changed_by: 'kumar', change_message: null,
      created_at: '2026-07-07T07:12:43.481893Z', deployed_at: '2026-07-07T07:54:13.644109Z',
    },
  ],
  2: [],
}

export const DEMO_DRIFT: Record<string, DriftStatus> = {
  '1:aria_socratic_system': {
    prompt_id: 1, prompt_name: 'aria_socratic_system',
    current_version_id: 1, current_version_number: 1,
    deployed_at: '2026-07-07T07:54:13.644109Z', quality_score: 0.93,
    recent_drift_severity: 'CRITICAL', changed_recently: true,
    root_cause_hint: 'Prompt v1 deployed within the last 7 days and quality has dropped (CRITICAL) — likely caused by that prompt change. Rollback recommended.',
  },
  '2:qaip_defect_explanation': {
    prompt_id: 2, prompt_name: 'qaip_defect_explanation',
    current_version_id: null, current_version_number: null,
    deployed_at: null, quality_score: null,
    recent_drift_severity: null, changed_recently: false,
    root_cause_hint: 'No deployed version for this prompt yet.',
  },
}

// Exact response captured from a real GET /metrics/business call against the
// live stack (see the assumptions fields for which numbers are computed from
// real counts vs. documented estimates — nothing here is invented beyond that).
export const DEMO_BUSINESS_METRICS: BusinessMetrics = {
  time_saved: {
    iterations_this_month: 1, manual_minutes: 30, automated_minutes: 2,
    saved_minutes: 28, saved_pct: 93.3,
    assumptions: { manual_minutes_per_iteration: 30, aipq_minutes_per_iteration: 2 },
  },
  incidents_prevented: {
    blocked_deployments: 0, avg_degradation_prevented: 0.0, estimated_impact_prevented: 0.0,
    assumptions: { sessions_per_deployment: 1000 },
  },
  rollback_speed: {
    manual_baseline_minutes: 180, aipq_avg_minutes: 0.0, improvement_pct: 100.0,
    automatic_rollback_count: 1,
  },
  quality_trend: {
    ARIA: [
      { date: '2026-07-06', avg_score: 0.9256 },
      { date: '2026-07-07', avg_score: 0.796 },
    ],
    QAIP: [],
  },
  coverage_gaps: [
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'jailbreak_resistance', score: 0.0 },
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'authority_pressure', score: 0.0 },
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'frustration_manipulation', score: 0.0 },
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'prompt_injection', score: 0.0 },
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'indirect_leakage', score: 0.0 },
    { project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system', category: 'multilingual_bypass', score: 0.0 },
  ],
  predictions: [
    {
      project_id: 1, project_name: 'ARIA', prompt_name: 'aria_socratic_system',
      days_until_risk: null, risk_level: 'LOW',
      recommendation: 'Not enough history (1/10 points) to forecast yet.',
    },
  ],
}
