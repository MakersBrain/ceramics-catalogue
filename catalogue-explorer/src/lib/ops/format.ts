/** Formatting shared by the /ops pages. */

export function relative(when: string | null | undefined, from = Date.now()): string {
	if (!when) return '—';
	const seconds = (from - new Date(when).getTime()) / 1000;
	const ahead = seconds < 0;
	const magnitude = Math.abs(seconds);
	const text = compact(magnitude);
	if (magnitude < 5) return 'just now';
	return ahead ? `in ${text}` : `${text} ago`;
}

export function compact(seconds: number): string {
	if (seconds < 60) return `${Math.round(seconds)}s`;
	if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
	if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
	return `${Math.round(seconds / 86400)}d`;
}

export function duration(started?: string | null, finished?: string | null): string {
	if (!started) return '—';
	const end = finished ? new Date(finished).getTime() : Date.now();
	return compact((end - new Date(started).getTime()) / 1000);
}

export function count(value: number | null | undefined): string {
	return value == null ? '—' : value.toLocaleString('en-GB');
}

/**
 * A record-count change against the previous run.
 *
 * Signed and proportional, because the absolute number says nothing on its own:
 * a source dropping 400 records matters enormously at 500 and not at all at
 * 40,000.
 */
export function delta(now: number | null | undefined, before: number | null | undefined) {
	if (now == null || before == null || before === 0) return null;
	const change = now - before;
	return { change, share: (100 * change) / before };
}

/** Colour for a source's staleness badge. */
export function staleness(seconds: number | null | undefined): {
	tone: string;
	label: string;
} {
	if (seconds == null) return { tone: 'badge-ghost', label: 'never' };
	if (seconds < 36 * 3600) return { tone: 'badge-success', label: compact(seconds) };
	if (seconds < 7 * 86400) return { tone: 'badge-warning', label: compact(seconds) };
	return { tone: 'badge-error', label: compact(seconds) };
}

export function stateTone(state: string): string {
	return (
		{
			succeeded: 'badge-success',
			complete: 'badge-success',
			running: 'badge-info',
			leased: 'badge-info',
			queued: 'badge-ghost',
			paused: 'badge-warning',
			degraded: 'badge-warning',
			failed: 'badge-error',
			cancelled: 'badge-ghost',
			skipped: 'badge-ghost'
		}[state] ?? 'badge-ghost'
	);
}

export function severityTone(severity: string): string {
	return (
		{ critical: 'badge-error', warning: 'badge-warning', info: 'badge-info' }[severity] ??
		'badge-ghost'
	);
}
