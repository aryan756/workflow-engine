interface Props {
  value: unknown
  label?: string
}

export function Json({ value, label }: Props) {
  if (value === null || value === undefined) {
    return <div className="json empty">— none —</div>
  }
  return (
    <div className="json-block">
      {label && <div className="json-label">{label}</div>}
      <pre className="json">{JSON.stringify(value, null, 2)}</pre>
    </div>
  )
}
