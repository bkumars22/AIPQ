import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type GoldenCaseSummary } from '../api/client'

function PatternListInput({ label, value, onChange }: { label: string; value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(value.join(', '))
  return (
    <label className="block text-xs text-slate-400">
      {label} <span className="text-slate-600">(comma-separated)</span>
      <input
        type="text"
        value={text}
        onChange={e => {
          setText(e.target.value)
          onChange(e.target.value.split(',').map(s => s.trim()).filter(Boolean))
        }}
        className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
      />
    </label>
  )
}

function CaseEditor({ initial, busy, onSave, onCancel }: {
  initial: Pick<GoldenCaseSummary, 'input_text' | 'expected_behavior' | 'category' | 'forbidden_patterns' | 'required_patterns'>
  busy: boolean
  onSave: (fields: typeof initial) => void
  onCancel: () => void
}) {
  const [fields, setFields] = useState(initial)
  return (
    <div className="space-y-2 rounded border border-slate-700 bg-slate-900/60 p-3">
      <label className="block text-xs text-slate-400">
        Input text
        <textarea
          value={fields.input_text}
          onChange={e => setFields({ ...fields, input_text: e.target.value })}
          rows={2}
          className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
        />
      </label>
      <label className="block text-xs text-slate-400">
        Expected behavior <span className="text-slate-600">(describe the expected reply shape, not the rule)</span>
        <textarea
          value={fields.expected_behavior}
          onChange={e => setFields({ ...fields, expected_behavior: e.target.value })}
          rows={2}
          className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
        />
      </label>
      <label className="block text-xs text-slate-400">
        Category
        <input
          type="text"
          value={fields.category}
          onChange={e => setFields({ ...fields, category: e.target.value })}
          className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <PatternListInput label="Required patterns" value={fields.required_patterns} onChange={v => setFields({ ...fields, required_patterns: v })} />
        <PatternListInput label="Forbidden patterns" value={fields.forbidden_patterns} onChange={v => setFields({ ...fields, forbidden_patterns: v })} />
      </div>
      <div className="flex gap-2 pt-1">
        <button
          onClick={() => onSave(fields)}
          disabled={busy || !fields.input_text || !fields.expected_behavior}
          className="px-3 py-1 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-40 disabled:cursor-not-allowed text-xs font-medium"
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button onClick={onCancel} className="px-3 py-1 rounded-md bg-slate-700 hover:bg-slate-600 text-xs font-medium">
          Cancel
        </button>
      </div>
    </div>
  )
}

export default function GoldenDatasetManager() {
  const { promptId: promptIdParam } = useParams<{ promptId: string }>()
  const promptId = Number(promptIdParam)
  const queryClient = useQueryClient()
  const [editingCaseId, setEditingCaseId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const { data: datasets, isLoading: datasetsLoading, error: datasetsError } = useQuery({
    queryKey: ['golden-datasets', promptId],
    queryFn: () => api.listGoldenDatasets(promptId),
  })

  // Only the first (lowest-id) dataset is ever real-evaluated -- see
  // GoldenDatasetSummary.is_active's backend docstring. Manage that one.
  const activeDataset = datasets?.find(d => d.is_active) ?? datasets?.[0]

  const { data: caseList, isLoading: casesLoading, error: casesError } = useQuery({
    queryKey: ['golden-cases', activeDataset?.id],
    queryFn: () => api.listGoldenCases(activeDataset!.id),
    enabled: activeDataset !== undefined,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['golden-datasets', promptId] })
    queryClient.invalidateQueries({ queryKey: ['golden-cases', activeDataset?.id] })
  }

  const createCase = useMutation({
    mutationFn: (fields: Pick<GoldenCaseSummary, 'input_text' | 'expected_behavior' | 'category' | 'forbidden_patterns' | 'required_patterns'>) =>
      api.createGoldenCase(promptId, fields.input_text, fields.expected_behavior, fields.category, fields.forbidden_patterns, fields.required_patterns),
    onSuccess: () => {
      setActionError(null)
      setCreating(false)
      invalidate()
    },
    onError: (err: Error) => setActionError(err.message),
  })

  const updateCase = useMutation({
    mutationFn: ({ caseId, fields }: { caseId: number; fields: Pick<GoldenCaseSummary, 'input_text' | 'expected_behavior' | 'category' | 'forbidden_patterns' | 'required_patterns'> }) =>
      api.updateGoldenCase(caseId, fields),
    onSuccess: () => {
      setActionError(null)
      setEditingCaseId(null)
      invalidate()
    },
    onError: (err: Error) => setActionError(err.message),
  })

  const deleteCase = useMutation({
    mutationFn: (caseId: number) => api.deleteGoldenCase(caseId),
    onSuccess: () => {
      setActionError(null)
      invalidate()
    },
    onError: (err: Error) => setActionError(err.message),
  })

  const categories = caseList ? [...new Set(caseList.cases.map(c => c.category))].sort() : []

  return (
    <div className="max-w-5xl mx-auto p-8">
      <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">&larr; All projects</Link>
      <h1 className="text-3xl font-bold mt-2 mb-1">Golden Dataset Manager</h1>
      <p className="text-slate-400 text-sm mb-6">Prompt #{promptId}</p>

      {(datasetsLoading || casesLoading) && <p className="text-slate-400">Loading…</p>}
      {datasetsError && <p className="text-red-400">Failed to load datasets: {(datasetsError as Error).message}</p>}
      {casesError && <p className="text-red-400">Failed to load cases: {(casesError as Error).message}</p>}

      {datasets && datasets.length === 0 && (
        <p className="text-slate-400">
          No golden dataset registered for this prompt yet — register one via the SDK's <code className="text-slate-300">register_prompt(golden_dataset=...)</code> first.
        </p>
      )}

      {datasets && datasets.length > 1 && (
        <div className="mb-4 rounded border border-amber-800 bg-amber-900/20 p-3 text-xs text-amber-400">
          This prompt has {datasets.length} golden_datasets rows. Only the first ever created
          (<span className="font-mono">{activeDataset?.name}</span>, id={activeDataset?.id}) is ever
          real-evaluated by the ai-engine — any others are silently never scored. The other
          {datasets.length > 2 ? ` ${datasets.length - 1} are` : ' one is'} shown for visibility only, not editable here.
        </div>
      )}

      {activeDataset && caseList && (
        <>
          <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4 mb-4 flex items-center justify-between">
            <div>
              <div className="font-mono text-sm text-slate-200">{activeDataset.name}</div>
              <div className="text-xs text-slate-400 mt-0.5">
                {activeDataset.case_count} cases across {categories.length} categories · threshold {activeDataset.threshold.toFixed(2)}
              </div>
            </div>
            <button
              onClick={() => setCreating(true)}
              className="px-3 py-1.5 rounded-md bg-sky-600 hover:bg-sky-500 text-sm font-medium"
            >
              + Add case
            </button>
          </div>

          {actionError && <p className="text-red-400 text-sm mb-3">{actionError}</p>}

          {creating && (
            <div className="mb-4">
              <CaseEditor
                initial={{ input_text: '', expected_behavior: '', category: 'baseline', forbidden_patterns: [], required_patterns: [] }}
                busy={createCase.isPending}
                onSave={fields => createCase.mutate(fields)}
                onCancel={() => setCreating(false)}
              />
            </div>
          )}

          <div className="space-y-2">
            {caseList.cases.map(c => (
              <div key={c.id} className="rounded-lg border border-slate-700 bg-slate-800/50 p-3">
                {editingCaseId === c.id ? (
                  <CaseEditor
                    initial={c}
                    busy={updateCase.isPending}
                    onSave={fields => updateCase.mutate({ caseId: c.id, fields })}
                    onCancel={() => setEditingCaseId(null)}
                  />
                ) : (
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-xs text-slate-500 mb-1">
                        <span className="font-mono">#{c.id}</span>
                        <span className="px-1.5 py-0.5 rounded bg-slate-700 text-slate-300">{c.category}</span>
                      </div>
                      <div className="text-sm text-slate-200 truncate">{c.input_text}</div>
                      <div className="text-xs text-slate-400 mt-1">{c.expected_behavior}</div>
                      {(c.required_patterns.length > 0 || c.forbidden_patterns.length > 0) && (
                        <div className="text-xs text-slate-500 mt-1 space-x-3">
                          {c.required_patterns.length > 0 && <span>requires: {c.required_patterns.join(', ')}</span>}
                          {c.forbidden_patterns.length > 0 && <span>forbids: {c.forbidden_patterns.join(', ')}</span>}
                        </div>
                      )}
                    </div>
                    <div className="flex gap-2 shrink-0">
                      <button
                        onClick={() => setEditingCaseId(c.id)}
                        className="px-2 py-1 rounded-md bg-slate-700 hover:bg-slate-600 text-xs font-medium"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => deleteCase.mutate(c.id)}
                        disabled={deleteCase.isPending}
                        className="px-2 py-1 rounded-md bg-red-900/60 hover:bg-red-800 disabled:opacity-40 text-xs font-medium text-red-300"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>

          {caseList.cases.length === 0 && !creating && (
            <p className="text-slate-400 text-sm">No cases in this dataset yet.</p>
          )}

          <p className="text-xs text-slate-600 mt-4">
            Editing a case's rubric doesn't itself invalidate the real-time scoring cache
            (keyed on prompt content + case id, not rubric text) — a version re-evaluated
            right after an edit may still need the cache cleared server-side to reflect it.
          </p>
        </>
      )}
    </div>
  )
}
