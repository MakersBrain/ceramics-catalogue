<script lang="ts">
	/**
	 * A number, what it is, and what would make someone act on it.
	 *
	 * The brand names this pattern (`.metric`, `.metric-label`, `.metric-value`)
	 * and its rule is the useful part: summary before detail, and never the
	 * number alone. A tile that says "3" and nothing else makes the reader open
	 * another page to find out whether 3 is bad.
	 *
	 * The value is Bitter with tabular figures - display face because it is the
	 * thing being read, tabular because a row of these should have its digits in
	 * columns and must not reflow as a live counter ticks.
	 */
	import type { Snippet } from 'svelte';
	import { cn } from '$lib/utils';

	let {
		label,
		value,
		detail,
		tone,
		class: className,
		children
	}: {
		label: string;
		value?: string | number;
		detail?: string;
		/** Applied to the value only: the label and detail stay readable. */
		tone?: string;
		class?: string;
		children?: Snippet;
	} = $props();
</script>

<div data-slot="metric" class={cn('bg-card flex flex-col gap-0.5 rounded-lg border p-4', className)}>
	<div class="eyebrow">{label}</div>
	{#if children}
		{@render children()}
	{:else}
		<div class={cn('font-heading text-2xl leading-tight font-semibold tabular-nums', tone)}>
			{value}
		</div>
	{/if}
	{#if detail}
		<div class="text-muted-foreground text-xs">{detail}</div>
	{/if}
</div>
