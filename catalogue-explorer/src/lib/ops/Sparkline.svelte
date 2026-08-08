<script lang="ts">
	/**
	 * A small time series. Inline SVG rather than a chart library: these are
	 * one series with no axes, legend or interaction, and layerchart earns its
	 * place on /explore rather than here.
	 */
	let {
		points,
		format = (value: number) => String(value),
		height = 64
	}: {
		points: { label: string; value: number }[];
		format?: (value: number) => string;
		height?: number;
	} = $props();

	const width = 480;
	const highest = $derived(Math.max(1, ...points.map((p) => p.value)));
	const step = $derived(points.length > 1 ? width / (points.length - 1) : width);

	const path = $derived(
		points
			.map((point, index) => {
				const x = index * step;
				const y = height - (height - 4) * (point.value / highest);
				return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ')
	);

	const area = $derived(points.length ? `${path} L${width},${height} L0,${height} Z` : '');
	const latest = $derived(points.at(-1));
</script>

{#if points.length === 0}
	<p class="text-base-content/50 text-sm">No data yet.</p>
{:else}
	<div class="flex items-baseline gap-2">
		<span class="text-2xl font-semibold tabular-nums">{format(latest?.value ?? 0)}</span>
		<span class="text-base-content/50 text-xs">{latest?.label}</span>
		<span class="text-base-content/40 ml-auto text-xs">peak {format(highest)}</span>
	</div>
	<svg
		viewBox="0 0 {width} {height}"
		class="mt-1 w-full"
		style="height: {height}px"
		preserveAspectRatio="none"
		role="img"
		aria-label="{points.length} day trend, latest {format(latest?.value ?? 0)}"
	>
		<path d={area} fill="currentColor" class="text-primary/15" />
		<path d={path} fill="none" stroke="currentColor" stroke-width="1.5" class="text-primary"
			vector-effect="non-scaling-stroke" />
	</svg>
{/if}
