<script lang="ts">
	export type Segment = { label: string; value: number };

	let {
		segments = [],
		format = (value: number) => value.toLocaleString('en-US')
	}: { segments: Segment[]; format?: (value: number) => string } = $props();

	// Fixed slot order, never cycled: an eight-hue ceiling is the token limit,
	// and the caller folds its tail into "other" before it gets here.
	const SLOTS = [
		'var(--series-1)',
		'var(--series-2)',
		'var(--series-3)',
		'var(--series-4)',
		'var(--series-5)',
		'var(--series-6)',
		'var(--series-7)',
		'var(--series-8)'
	];

	const total = $derived(segments.reduce((sum, segment) => sum + segment.value, 0));
	const share = (value: number) => (total ? (value / total) * 100 : 0);

	let hovered: number | null = $state(null);
</script>

<div>
	<!-- 2px surface gaps do the separating; no strokes around the segments. -->
	<div class="flex h-8 w-full gap-0.5" role="img" aria-label="Share of catalogue by family">
		{#each segments as segment, index (segment.label)}
			<button
				type="button"
				class="h-full first:rounded-l last:rounded-r"
				style="
					flex: {share(segment.value)} 1 0;
					background: {SLOTS[index % SLOTS.length]};
					opacity: {hovered === null || hovered === index ? 1 : 0.65};
				"
				aria-label="{segment.label}: {format(segment.value)} products, {share(
					segment.value
				).toFixed(1)}%"
				onpointerenter={() => (hovered = index)}
				onpointerleave={() => (hovered = null)}
				onfocus={() => (hovered = index)}
				onblur={() => (hovered = null)}
			></button>
		{/each}
	</div>

	<!-- Legend is the dependable identity channel; the swatch carries the hue so
	     the text can stay in an ink token. -->
	<ul class="mt-4 flex flex-wrap gap-x-5 gap-y-2">
		{#each segments as segment, index (segment.label)}
			<li class="flex items-center gap-2 text-xs">
				<span
					class="inline-block h-2.5 w-2.5 rounded-sm"
					style="background: {SLOTS[index % SLOTS.length]}"
				></span>
				<span style="color: {hovered === index ? 'var(--text-primary)' : 'var(--text-secondary)'}">
					{segment.label}
				</span>
				<span class="tabular-nums" style="color: var(--text-muted)">
					{share(segment.value).toFixed(1)}%
				</span>
			</li>
		{/each}
	</ul>
</div>
