import { useState } from 'react'

export interface Preset {
  key: string
  label: string
  hint: string
  input: {
    customer_id: string
    subject: string
    message: string
    channel: string
  }
  options?: Record<string, unknown>
}

export const PRESETS: Preset[] = [
  {
    key: 'bug',
    label: 'Bug report',
    hint: 'routes to the bug path → files a Linear issue',
    input: {
      customer_id: 'cus_1001',
      subject: 'Dashboard crashes with a 500 error on export',
      message:
        'Every time I click Export on the analytics dashboard the page crashes and I get a 500 error. This is broken for my whole team since the release yesterday.',
      channel: 'email',
    },
  },
  {
    key: 'billing',
    label: 'Billing question',
    hint: 'routes to the billing path → looks up the invoice',
    input: {
      customer_id: 'cus_1002',
      subject: 'Invoice charge looks wrong',
      message:
        'We were charged twice on our last invoice. Can you check the payment and issue a refund for the duplicate charge on our subscription?',
      channel: 'email',
    },
  },
  {
    key: 'unclear',
    label: 'Ambiguous ticket',
    hint: 'low confidence → pauses for human approval',
    input: {
      customer_id: 'cus_1003',
      subject: 'Question',
      message: 'Hi, can someone get back to me about the thing we discussed?',
      channel: 'email',
    },
  },
  {
    key: 'invalid',
    label: 'Invalid input',
    hint: 'fails the intake contract before anything runs',
    input: { customer_id: '', subject: '', message: '', channel: 'carrier-pigeon' },
  },
]

export const FAULTS = [
  { key: 'none', label: 'No fault', options: {} },
  {
    key: 'tool_retry',
    label: 'create_issue: transient failure ×1 (auto-recovers)',
    options: { faults: { create_issue: { kind: 'tool_transient', times: 1 } } },
  },
  {
    key: 'tool_fail',
    label: 'create_issue: transient failure ×2 (needs manual retry)',
    options: { faults: { create_issue: { kind: 'tool_transient', times: 2 } } },
  },
  {
    key: 'idempotency',
    label: 'create_issue: fails AFTER the side effect (idempotency demo)',
    options: { faults: { create_issue: { kind: 'tool_after_side_effect', times: 2 } } },
  },
  {
    key: 'agent_invalid',
    label: 'classify: agent returns schema-violating output',
    options: { faults: { classify: { kind: 'agent_invalid_output', times: 2 } } },
  },
]

interface Props {
  busy: boolean
  /** The server drops `options.faults` when this is off, so hide the control. */
  faultInjectionEnabled: boolean
  onSubmit: (input: Record<string, unknown>, options: Record<string, unknown>) => void
}

export function NewRunForm({ busy, faultInjectionEnabled, onSubmit }: Props) {
  const [presetKey, setPresetKey] = useState(PRESETS[0].key)
  const [faultKey, setFaultKey] = useState(FAULTS[0].key)
  const [form, setForm] = useState(PRESETS[0].input)

  const applyPreset = (key: string) => {
    setPresetKey(key)
    const preset = PRESETS.find((p) => p.key === key)
    if (preset) setForm(preset.input)
  }

  const preset = PRESETS.find((p) => p.key === presetKey)!
  const fault = faultInjectionEnabled
    ? FAULTS.find((f) => f.key === faultKey)!
    : FAULTS[0]

  return (
    <form
      className="panel new-run"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit(form, fault.options)
      }}
    >
      <h2>Submit a request</h2>

      <label>
        Scenario
        <select value={presetKey} onChange={(e) => applyPreset(e.target.value)}>
          {PRESETS.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      <p className="hint">{preset.hint}</p>

      <label>
        Customer id
        <input
          value={form.customer_id}
          onChange={(e) => setForm({ ...form, customer_id: e.target.value })}
          placeholder="cus_1001"
        />
      </label>

      <label>
        Subject
        <input
          value={form.subject}
          onChange={(e) => setForm({ ...form, subject: e.target.value })}
        />
      </label>

      <label>
        Message
        <textarea
          rows={5}
          value={form.message}
          onChange={(e) => setForm({ ...form, message: e.target.value })}
        />
      </label>

      {faultInjectionEnabled && (
        <label>
          Fault injection
          <select value={faultKey} onChange={(e) => setFaultKey(e.target.value)}>
            {FAULTS.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
      )}

      <button type="submit" className="primary" disabled={busy}>
        {busy ? 'Starting…' : 'Start run'}
      </button>
    </form>
  )
}
