<script lang="ts">
	import { scaleBand } from 'd3-scale';
	import { BarChart as Bars, Tooltip } from 'layerchart';

	export type BarItem = { label: string; value: number; note?: string };

	/**
	 * A ranked comparison, on a scale the reader can actually read.
	 *
	 * The hand-rolled version this replaces drew the bars correctly but had no
	 * axis, so a bar could only be compared with the bar beside it - "twice as
	 * long as that one" was the only fact available, never "about 4,000". The
	 * value axis and its gridlines are the whole reason for the change; LayerChart
	 * supplies the scale, the ticks and the band layout, and the palette, the
	 * emphasis rule and the tooltip stay exactly as they were.
	 */

	let {
		items = [],
		format = (value: number) => value.toLocaleString('en-US'),
		/** Index of the one bar the story is about; the rest recede to gray. */
		emphasis = -1,
		/** Pixels reserved for the category labels down the left. */
		labelWidth = 176
	}: {
		items: BarItem[];
		format?: (value: number) => string;
		emphasis?: number;
		labelWidth?: number;
	} = $props();

	/**
	 * Position is the identity, not the label: two suppliers can legitimately
	 * share a name in a filtered selection, and the band scale needs distinct
	 * keys or it would collapse them onto one bar.
	 */
	const rows = $derived(
		items.map((item, index) => ({
			...item,
			key: `${index}`,
			tone: emphasis < 0 ? 'series' : index === emphasis ? 'accent' : 'recede'
		}))
	);

	/**
	 * A single hue carries magnitude; length is what the reader compares. The
	 * emphasis form only kicks in when one bar is the point of the chart.
	 */
	const TONES: Record<string, string> = {
		series: 'var(--series-1)',
		accent: 'var(--accent)',
		recede: 'var(--recede)'
	};

	/**
	 * Colour is a property of a series here, not of a datum - a bar takes its
	 * fill from the series it belongs to and ignores any colour scale. So the two
	 * emphasis tones are two series over the same bands, each reading a value
	 * only for the rows it owns. Overlapping them puts every bar back on its own
	 * band, which is what the reader sees: one list, one bar each.
	 */
	const series = $derived(
		(emphasis < 0 ? ['series'] : ['accent', 'recede']).map((tone) => ({
			key: tone,
			color: TONES[tone],
			value: (row: (typeof rows)[number]) => (row.tone === tone ? row.value : null)
		}))
	);

	// Bars are a fixed height and the chart grows to fit them, rather than the
	// bars stretching to fill a fixed chart: ten suppliers and three suppliers
	// should draw the same bar, not one three times the other's thickness.
	const height = $derived(Math.max(rows.length * 28 + 36, 96));

	/**
	 * How wide the chart actually is, so the label gutter can be a share of it
	 * rather than a fixed count of pixels.
	 *
	 * `labelWidth` is chosen for the widest label a panel expects - 240 on the
	 * comparison page, where a label is "supplier - 4.5 l". On a phone that left
	 * about a hundred pixels for the bars themselves, so every bar in the chart
	 * was a stub and the length comparison the chart exists for was gone. The
	 * caller's figure is now a ceiling: the gutter never takes more than half the
	 * width, so the bars always have the longer half.
	 */
	let width = $state(0);

	const gutter = $derived(width > 0 ? Math.min(labelWidth, Math.max(64, width * 0.42)) : labelWidth);

	/**
	 * SVG text does not clip itself, so a label longer than the gutter would run
	 * straight over the bars. Cut it to what fits, at roughly 5.6px per character
	 * for the 11px face the axis uses. The full text is on the tooltip.
	 */
	const chars = $derived(Math.max(6, Math.floor((gutter - 8) / 5.6)));

	const clip = $derived((label: string) =>
		label.length > chars ? `${label.slice(0, chars - 1)}…` : label
	);

	/**
	 * How many ticks the value axis can carry without its labels colliding.
	 *
	 * Four is right on a laptop and unreadable on a phone: a currency format is
	 * about seventy pixels of text, so four of them across the 170px a phone
	 * leaves for the bars comes out as one smear along the bottom of the chart.
	 * Two ticks and a gridline are still a scale; overlapping labels are not.
	 */
	const ticks = $derived(
		Math.max(2, Math.min(4, Math.floor(Math.max(0, width - gutter - 12) / 70)))
	);
</script>

<div style="height: {height}px" bind:clientWidth={width}>
	<Bars
		data={rows}
		orientation="horizontal"
		x="value"
		y="key"
		yScale={scaleBand().padding(0.25)}
		{series}
		seriesLayout="overlap"
		padding={{ left: gutter, bottom: 24, top: 4, right: 12 }}
		axis
		grid
		props={{
			bars: { radius: 2, strokeWidth: 0 },
			// Ticks along the value axis with a rule under them; the category axis
			// carries names, so it needs neither ticks nor a line of its own.
			xAxis: {
				format,
				ticks,
				rule: { class: 'stroke-[var(--baseline)]' },
				classes: { tickLabel: 'fill-[var(--text-muted)] text-[10px]' }
			},
			yAxis: {
				format: (key: string) => clip(rows[Number(key)]?.label ?? ''),
				tickLength: 0,
				rule: false,
				classes: { tickLabel: 'fill-[var(--text-secondary)] text-[11px]' }
			},
			// Gridlines run along the value axis only. A line between two
			// categories would divide names that are already separate.
			grid: { x: true, y: false, class: 'stroke-[var(--gridline)]' },
			highlight: { area: { class: 'fill-[var(--accent)] opacity-10' } }
		}}
	>
		{#snippet tooltip()}
			<Tooltip.Root>
				{#snippet children({ data }: { data: (typeof rows)[number] })}
					<div
						class="rounded-lg px-3 py-2 text-xs shadow-lg"
						style="background: var(--surface-1); border: 1px solid var(--hairline); min-width: 9rem"
					>
						<div class="flex items-center gap-2">
							<span class="h-0.5 w-4 rounded" style="background: {TONES[data.tone]}"></span>
							<span style="color: var(--text-secondary)">{data.label}</span>
						</div>
						<div
							class="mt-1 text-sm font-semibold tabular-nums"
							style="color: var(--text-primary)"
						>
							{format(data.value)}
						</div>
						{#if data.note}
							<div class="mt-0.5" style="color: var(--text-muted)">{data.note}</div>
						{/if}
					</div>
				{/snippet}
			</Tooltip.Root>
		{/snippet}
	</Bars>
</div>
