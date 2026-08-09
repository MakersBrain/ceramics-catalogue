<script lang="ts">
	/**
	 * One shop's price for one product, over time.
	 *
	 * Stepped rather than sloped: a price does not drift from 6.90 to 7.40 over
	 * a week, it is 6.90 until the day it is 7.40. A straight line between the
	 * two observations would draw days at prices the shop never charged.
	 *
	 * One series, so no legend — the heading names it. Inline SVG keeps it in
	 * step with Sparkline and AvailabilityBand and avoids a scale library for
	 * two linear scales.
	 */

	export type Point = { at: string | Date; value: number };

	let {
		points,
		currency = '',
		height = 132,
		label = 'Price'
	}: {
		points: Point[];
		currency?: string;
		height?: number;
		label?: string;
	} = $props();

	const PADDING = { top: 10, right: 8, bottom: 20, left: 46 };
	const WIDTH = 460;

	const time = (value: string | Date) => new Date(value).getTime();

	/**
	 * One reading per moment, not one per observation.
	 *
	 * A shop that lists several pack sizes on one page contributes several
	 * prices against the same product at the same instant — Esmaltes y
	 * Colorantes has ten, from 4.84 to 10.03. Plotted as a series those become a
	 * vertical smear at each crawl and a line through the middle of nothing,
	 * which reads as violent price movement and is really a size range.
	 *
	 * So each moment collapses to its low, high and middle. The line follows the
	 * middle; the band behind it is the spread, and it disappears entirely when
	 * a shop quotes one price, which is the common case.
	 */
	/**
	 * Observations within this of each other belong to the same visit. A crawl
	 * walks a shop over minutes, stamping each page as it reads it, so the ten
	 * prices from one product page arrive seconds apart and are one reading of
	 * one moment — not ten moments. Two hours is far longer than a source takes
	 * and far shorter than the gap between runs.
	 */
	const SAME_VISIT_MS = 2 * 60 * 60 * 1000;

	const ordered = $derived.by(() => {
		const usable = points
			.filter((point) => Number.isFinite(point.value))
			.map((point) => ({ at: time(point.at), value: point.value }))
			.sort((a, b) => a.at - b.at);

		const visits: { at: number; values: number[] }[] = [];
		for (const point of usable) {
			const last = visits[visits.length - 1];
			if (last && point.at - last.at <= SAME_VISIT_MS) last.values.push(point.value);
			else visits.push({ at: point.at, values: [point.value] });
		}

		return visits.map((visit) => {
			const sorted = [...visit.values].sort((a, b) => a - b);
			return {
				at: visit.at,
				low: sorted[0],
				high: sorted[sorted.length - 1],
				// The median rather than the mean: with two pack sizes the mean is
				// a price the shop does not charge for anything.
				value: sorted[Math.floor((sorted.length - 1) / 2)],
				count: sorted.length
			};
		});
	});

	/** Whether any moment carried more than one price, so the band means something. */
	const spread = $derived(ordered.some((entry) => entry.count > 1));

	const scales = $derived.by(() => {
		if (ordered.length === 0) return null;
		const times = ordered.map((p) => p.at);
		// The band has to fit, not just the line.
		const values = ordered.flatMap((p) => [p.low, p.high]);
		const t0 = Math.min(...times);
		const t1 = Math.max(...times);
		let low = Math.min(...values);
		let high = Math.max(...values);
		if (high === low) {
			// A price that never moved still deserves a readable line rather than
			// one welded to an axis. Give it room either side of the value.
			const nudge = Math.abs(high) * 0.1 || 1;
			low -= nudge;
			high += nudge;
		} else {
			const margin = (high - low) * 0.15;
			low -= margin;
			high += margin;
		}
		const plotWidth = WIDTH - PADDING.left - PADDING.right;
		const plotHeight = height - PADDING.top - PADDING.bottom;
		return {
			t0,
			t1,
			low,
			high,
			x: (t: number) => PADDING.left + (t1 === t0 ? plotWidth : ((t - t0) / (t1 - t0)) * plotWidth),
			y: (v: number) => PADDING.top + plotHeight - ((v - low) / (high - low)) * plotHeight,
			plotWidth,
			plotHeight
		};
	});

	const placed = $derived(
		scales
			? ordered.map((p) => ({
					...p,
					cx: scales.x(p.at),
					cy: scales.y(p.value),
					yLow: scales.y(p.low),
					yHigh: scales.y(p.high)
				}))
			: []
	);

	/** The spread as a closed shape: highs left to right, then lows back again. */
	const bandPath = $derived.by(() => {
		if (!spread || placed.length === 0) return '';
		const highs = placed.map((p, i) =>
			i === 0 ? `M ${p.cx} ${p.yHigh}` : `L ${placed[i - 1].cx} ${p.yHigh} L ${p.cx} ${p.yHigh}`
		);
		const lows = [...placed]
			.reverse()
			.map((p, i, all) =>
				i === 0 ? `L ${p.cx} ${p.yLow}` : `L ${all[i - 1].cx} ${p.yLow} L ${p.cx} ${p.yLow}`
			);
		return `${highs.join(' ')} ${lows.join(' ')} Z`;
	});

	/** Step-after: hold the old price across, then drop or rise at the observation. */
	const path = $derived.by(() => {
		if (placed.length === 0) return '';
		let d = `M ${placed[0].cx} ${placed[0].cy}`;
		for (let i = 1; i < placed.length; i += 1) {
			d += ` L ${placed[i].cx} ${placed[i - 1].cy} L ${placed[i].cx} ${placed[i].cy}`;
		}
		return d;
	});

	/** Three ticks: the two ends of the domain and the middle. */
	const ticks = $derived(
		scales
			? [scales.high, (scales.high + scales.low) / 2, scales.low].map((value) => ({
					value,
					y: scales.y(value)
				}))
			: []
	);

	let hovered = $state<number | null>(null);

	const money = (value: number) =>
		`${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? ` ${currency}` : ''}`;

	const day = (value: string | Date | number) =>
		new Date(value).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });

	const moment = (value: string | Date | number) =>
		new Date(value).toLocaleString(undefined, {
			day: 'numeric',
			month: 'short',
			hour: '2-digit',
			minute: '2-digit'
		});

	/** The nearest observation to the pointer, so the whole plot is a hit target. */
	function track(event: PointerEvent) {
		if (!scales || placed.length === 0) return;
		const box = (event.currentTarget as SVGSVGElement).getBoundingClientRect();
		const x = ((event.clientX - box.left) / box.width) * WIDTH;
		let best = 0;
		for (let i = 1; i < placed.length; i += 1) {
			if (Math.abs(placed[i].cx - x) < Math.abs(placed[best].cx - x)) best = i;
		}
		hovered = best;
	}

	/**
	 * First middle against last middle. Comparing raw first and last
	 * observations would report the gap between two pack sizes as a price rise.
	 */
	const change = $derived.by(() => {
		if (placed.length < 2) return null;
		const first = placed[0].value;
		const last = placed[placed.length - 1].value;
		if (first === last || first === 0) return null;
		return { delta: last - first, pct: ((last - first) / first) * 100 };
	});
</script>

{#if placed.length}
	<figure class="m-0">
		<figcaption class="flex items-baseline justify-between">
			<span class="text-xs font-semibold" style="color: var(--text-secondary)">
				{label} ({placed.length} reading{placed.length === 1 ? '' : 's'})
			</span>
			{#if change}
				<span class="text-[11px] tabular-nums" style="color: var(--text-secondary)">
					{change.delta > 0 ? '+' : ''}{money(change.delta)}
					({change.pct > 0 ? '+' : ''}{change.pct.toFixed(1)}%) since {day(placed[0].at)}
				</span>
			{/if}
		</figcaption>

		<svg
			class="mt-1 w-full"
			viewBox="0 0 {WIDTH} {height}"
			role="img"
			aria-label="{label} over time, {placed.length} observations from {moment(
				placed[0].at
			)} to {moment(placed[placed.length - 1].at)}"
			onpointermove={track}
			onpointerleave={() => (hovered = null)}
		>
			<!-- Recessive grid: present enough to read a value against, quiet
			     enough that the line is what the eye lands on. -->
			{#each ticks as tick (tick.value)}
				<line
					x1={PADDING.left}
					x2={WIDTH - PADDING.right}
					y1={tick.y}
					y2={tick.y}
					stroke="var(--gridline)"
					stroke-width="1"
				/>
				<text
					x={PADDING.left - 6}
					y={tick.y + 3}
					text-anchor="end"
					font-size="9"
					fill="var(--text-muted)"
				>
					{tick.value.toFixed(2)}
				</text>
			{/each}

			{#if bandPath}
				<!-- The spread of what the shop quoted at each crawl, behind the
				     middle of it. Soft, because it is context for the line rather
				     than a second series. -->
				<path d={bandPath} fill="var(--accent)" fill-opacity="0.16" stroke="none" />
			{/if}

			<path d={path} fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" />

			{#each placed as point, index (index)}
				<circle
					cx={point.cx}
					cy={point.cy}
					r={hovered === index ? 5 : 4}
					fill="var(--accent)"
					stroke="var(--surface-1)"
					stroke-width="2"
				/>
			{/each}

			{#if hovered !== null && placed[hovered]}
				<line
					x1={placed[hovered].cx}
					x2={placed[hovered].cx}
					y1={PADDING.top}
					y2={height - PADDING.bottom}
					stroke="var(--baseline)"
					stroke-width="1"
				/>
			{/if}

			<text x={PADDING.left} y={height - 6} font-size="9" fill="var(--text-muted)">
				{day(placed[0].at)}
			</text>
			<text
				x={WIDTH - PADDING.right}
				y={height - 6}
				text-anchor="end"
				font-size="9"
				fill="var(--text-muted)"
			>
				{day(placed[placed.length - 1].at)}
			</text>
		</svg>

		{#if hovered !== null && placed[hovered]}
			<p class="mt-1 text-[11px] tabular-nums" style="color: var(--text-primary)">
				{placed[hovered].count > 1
					? `${money(placed[hovered].low)} to ${money(placed[hovered].high)}`
					: money(placed[hovered].value)}
				<span style="color: var(--text-muted)">
					- {moment(placed[hovered].at)}{placed[hovered].count > 1
						? ` - ${placed[hovered].count} prices quoted`
						: ''}
				</span>
			</p>
		{/if}
		{#if spread}
			<p class="mt-1 text-[10px]" style="color: var(--text-muted)">
				This shop quotes several prices for this product at once — pack sizes it lists on one
				page. The line is the middle one and the band is the range, so the shading is size, not
				a price that moved.
			</p>
		{/if}
	</figure>
{/if}
