type BadgeProps = { value: string; variant?: string }

export function Badge({ value, variant = value }: BadgeProps) {
  return <span className={`badge badge--${variant.replaceAll('_', '-')}`}>{value.replaceAll('_', ' ')}</span>
}
