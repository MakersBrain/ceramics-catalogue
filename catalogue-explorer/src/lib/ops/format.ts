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

/**
 * The tones below are `StatusBadge` values, not class names.
 *
 * They used to be daisyUI badge classes, which meant a helper in the formatting
 * module decided what a failed run looked like. Naming the outcome instead
 * leaves that decision in one component, and it is why the ops pages could move
 * off daisyUI without re-deciding the palette in nine files.
 *
 * `busy` covers what was `badge-info`: work in progress is not an outcome and
 * must not borrow a colour that means one.
 */
export type Tone = 'neutral' | 'good' | 'bad' | 'warn' | 'busy';

/** Tone for a source's staleness badge. */
export function staleness(seconds: number | null | undefined): {
	tone: Tone;
	label: string;
} {
	if (seconds == null) return { tone: 'neutral', label: 'never' };
	if (seconds < 36 * 3600) return { tone: 'good', label: compact(seconds) };
	if (seconds < 7 * 86400) return { tone: 'warn', label: compact(seconds) };
	return { tone: 'bad', label: compact(seconds) };
}

export function stateTone(state: string): Tone {
	return (
		{
			succeeded: 'good',
			complete: 'good',
			running: 'busy',
			leased: 'busy',
			queued: 'neutral',
			paused: 'warn',
			degraded: 'warn',
			failed: 'bad',
			cancelled: 'neutral',
			skipped: 'neutral'
		}[state] ?? 'neutral'
	) as Tone;
}

export function severityTone(severity: string): Tone {
	return ({ critical: 'bad', warning: 'warn', info: 'busy' }[severity] ?? 'neutral') as Tone;
}
