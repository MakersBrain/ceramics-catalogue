<script lang="ts">
	/**
	 * What a shop said it had, over time.
	 *
	 * A band rather than a line, because availability is a state that holds
	 * until it changes rather than a value that moves: a shop is out of stock
	 * *between* the observation that said so and the next one, and drawing it
	 * as points would leave the reader to imagine the interval. Each segment is
	 * therefore drawn from its observation to the next one, and the last runs
	 * to the moment of the most recent crawl and no further — the catalogue
	 * knows nothing about now, only about when it last looked.
	 *
	 * Inline SVG, on the same reasoning as Sparkline: one row of rectangles with
	 * no axes or scales to speak of, where layerchart would be more machinery
	 * than the drawing needs.
	 */

	export type Point = { at: string | Date; state: string | null };

	let {
		points,
		height = 26
	}: {
		points: Point[];
		height?: number;
	} = $props();

	/**
	 * schema.org URLs to the four states worth telling apart. Anything else a
	 * shop publishes — and shops publish typos — reads as unknown rather than
	 * being forced into one of these.
	 */
	const STATES: Record<string, { label: string; token: string; glyph: string }> = {
		InStock: { label: 'In stock', token: 'var(--good)', glyph: '●' },
		OutOfStock: { label: 'Out of stock', token: 'var(--critical)', glyph: '×' },
		SoldOut: { label: 'Sold out', token: 'var(--critical)', glyph: '×' },
		LimitedAvailability: { label: 'Limited', token: 'var(--warning)', glyph: '◐' },
		BackOrder: { label: 'On back order', token: 'var(--warning)', glyph: '◐' },
		PreOrder: { label: 'Pre-order', token: 'var(--warning)', glyph: '◐' }
	};

	const UNKNOWN = { label: 'Not published', token: 'var(--recede)', glyph: '?' };

	function classify(state: string | null) {
		if (!state) return UNKNOWN;
		// "https://schema.org/InStock" and the bare "InStock" both occur.
		const tail = state.split('/').pop() ?? '';
		return STATES[tail] ?? UNKNOWN;
	}

	const time = (value: string | Date) => new Date(value).getTime();

	/** Oldest first: a timeline reads left to right whatever order it arrived in. */
	const ordered = $derived([...points].sort((a, b) => time(a.at) - time(b.at)));

	const span = $derived.by(() => {
		if (ordered.length === 0) return null;
		const start = time(ordered[0].at);
		const end = time(ordered[ordered.length - 1].at);
		// A single observation, or several inside one second, has no width to
		// scale against. Give it a nominal day so it draws as one full segment
		// rather than dividing by zero.
		return { start, end: end > start ? end : start + 86_400_000 };
	});

	const segments = $derived.by(() => {
		if (!span) return [];
		const width = span.end - span.start;

		// Runs of the same state become one rectangle. Drawing one per
		// observation left a seam everywhere the crawler happened to look, and a
		// seam in a status band reads as a change of status — a shop that was in
		// stock all week looked like it had been in and out of it nine times.
		const runs: { from: number; to: number; state: ReturnType<typeof classify>; at: string | Date }[] =
			[];
		for (const [index, point] of ordered.entries()) {
			const from = time(point.at);
			const to = index + 1 < ordered.length ? time(ordered[index + 1].at) : span.end;
			const state = classify(point.state);
			const last = runs[runs.length - 1];
			if (last && last.state.label === state.label) last.to = to;
			else runs.push({ from, to, state, at: point.at });
		}

		return runs.map((run) => ({
			x: ((run.from - span.start) / width) * 100,
			// A floor, so a state that held for minutes is still visible on a band
			// whose full width is weeks.
			width: Math.max(((run.to - run.from) / width) * 100, 0.6),
			state: run.state,
			at: run.at,
			until: run.to
		}));
	});

	/** Only the states actually present, in the order they are defined above. */
	const legend = $derived.by(() => {
		const seen = new Map<string, { label: string; token: string; glyph: string }>();
		for (const segment of segments) seen.set(segment.state.label, segment.state);
		return [...seen.values()];
	});

	const day = (value: string | Date) =>
		new Date(value).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });

	const moment = (value: string | Date | number) =>
		new Date(value).toLocaleString(undefined, {
			day: 'numeric',
			month: 'short',
			hour: '2-digit',
			minute: '2-digit'
		});
</script>

{#if segments.length}
	<div>
		<div class="relative w-full overflow-hidden rounded" style="height: {height}px">
			{#each segments as segment, index (index)}
				<!-- title, not a bespoke tooltip: the band is a row of adjacent
				     rectangles, and the browser's own is both accessible and
				     enough for "what was it, and when". -->
				<div
					class="absolute top-0 h-full"
					style="left: {segment.x}%; width: {segment.width}%; background: {segment.state.token}"
					title="{segment.state.label} - from {moment(segment.at)} to {moment(segment.until)}"
				></div>
			{/each}
		</div>

		<div
			class="mt-1 flex justify-between text-[10px] tabular-nums"
			style="color: var(--text-muted)"
		>
			<span>{day(ordered[0].at)}</span>
			<span>{day(ordered[ordered.length - 1].at)}</span>
		</div>

		<!-- The word and the glyph, so the state never rides on hue alone. -->
		<ul class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]" style="color: var(--text-secondary)">
			{#each legend as entry (entry.label)}
				<li class="flex items-center gap-1.5">
					<span aria-hidden="true" style="color: {entry.token}">{entry.glyph}</span>
					{entry.label}
				</li>
			{/each}
		</ul>
	</div>
{/if}
