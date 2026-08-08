<script lang="ts">
	import { scaleBand } from 'd3-scale';
	import { Axis, Chart, Circle, Grid, Group, Line, Rect, Svg } from 'layerchart';
	import { goto } from '$app/navigation';

	export type Range = {
		label: string;
		sublabel?: string;
		low: number;
		high: number;
		note?: string;
		href?: string;
	};

	/**
	 * What the same product costs at its cheapest and dearest supplier.
	 *
	 * A dumbbell rather than two bars: the reader's question is how far apart the
	 * two ends are, and a gap is read directly where two bar lengths have to be
	 * subtracted. The change from the hand-rolled version is the axis underneath.
	 * Before, a dot's position meant only "further right than that other dot";
	 * now it means a price, which is what somebody deciding where to buy needs.
	 *
	 * The scale is shared by every row, so a wide bar is a wide spread rather
	 * than a differently-scaled row.
	 */

	let {
		items = [],
		format = (value: number) => value.toFixed(2)
	}: { items: Range[]; format?: (value: number) => string } = $props();

	/** Position is the identity: two rows can carry the same code. */
	const rows = $derived(items.map((item, index) => ({ ...item, key: `${index}` })));

	const floor = $derived(Math.min(...rows.map((row) => row.low), Infinity));
	const ceiling = $derived(Math.max(...rows.map((row) => row.high), 0));

	/**
	 * A little air at both ends so a dot sitting on the cheapest price in the set
	 * is not drawn half off the axis.
	 */
	const domain = $derived.by(() => {
		const span = Math.max(ceiling - floor, 1e-9);
		return [floor - span * 0.04, ceiling + span * 0.04];
	});

	const height = $derived(Math.max(rows.length * 30 + 34, 110));
</script>

<div style="height: {height}px">
	<Chart
		data={rows}
		x={(row: (typeof rows)[number]) => row.high}
		xDomain={domain}
		y="key"
		yScale={scaleBand().padding(0.35)}
		padding={{ left: 148, bottom: 26, top: 4, right: 56 }}
	>
		{#snippet children({ context }: { context: any })}
			<Svg>
				<Grid x class="stroke-[var(--gridline)]" />
				<Axis
					placement="bottom"
					{format}
					ticks={5}
					rule={{ class: 'stroke-[var(--baseline)]' }}
					classes={{ tickLabel: 'fill-[var(--text-muted)] text-[10px]' }}
				/>
				<Axis
					placement="left"
					format={(key: string) => rows[Number(key)]?.label ?? ''}
					tickLength={0}
					rule={false}
					classes={{ tickLabel: 'fill-[var(--text-primary)] text-[11px]' }}
				/>

				{#each rows as row (row.key)}
					{@const band = context.yScale(row.key)}
					{@const y = band + context.yScale.bandwidth() / 2}
					{@const left = context.xScale(row.low)}
					{@const right = context.xScale(row.high)}
					<!--
						Everything here is a LayerChart primitive rather than a raw <g> or
						<a>. Svelte works out an element's namespace from the literal tag
						enclosing it, and inside a component's snippet there is no such
						tag - a hand-written <a> is built as an HTML anchor sitting in an
						SVG, which lays out to nothing and draws nothing. These components
						create their own SVG nodes and are immune to that.
					-->
					<Group
						role="link"
						tabindex={0}
						aria-label="{row.label}{row.sublabel ? ` - ${row.sublabel}` : ''}: {format(
							row.low
						)} to {format(row.high)}{row.note ? ` (${row.note})` : ''}"
						class="cursor-pointer"
						onclick={() => row.href && goto(row.href)}
						onkeydown={(event: KeyboardEvent) => {
							if (row.href && (event.key === 'Enter' || event.key === ' ')) {
								event.preventDefault();
								goto(row.href);
							}
						}}
					>
						<!-- The whole band is the hit target, so a pointer never has to
						     find a two-pixel connector. -->
						<Rect
							x={0}
							y={band}
							width={Math.max(context.width, 0)}
							height={context.yScale.bandwidth()}
							class="fill-transparent"
						/>
						<!-- Connector, then the two ends: one hue, two shades. -->
						<Line
							x1={left}
							x2={right}
							y1={y}
							y2={y}
							class="stroke-[var(--accent-soft)]"
							stroke-width={2}
						/>
						<Circle cx={left} cy={y} r={5} class="fill-[var(--accent)]" />
						<Circle cx={right} cy={y} r={5} class="fill-[var(--accent-soft)]" />
					</Group>
				{/each}
			</Svg>
		{/snippet}
	</Chart>
</div>

<!-- The legend is the dependable identity channel: which end is which never
     rides on the shade alone. -->
<div class="mt-3 flex flex-wrap items-center gap-4 text-xs" style="color: var(--text-secondary)">
	<span class="flex items-center gap-2">
		<span class="inline-block h-2.5 w-2.5 rounded-full" style="background: var(--accent)"></span>
		cheapest supplier
	</span>
	<span class="flex items-center gap-2">
		<span class="inline-block h-2.5 w-2.5 rounded-full" style="background: var(--accent-soft)"
		></span>
		dearest supplier
	</span>
</div>
