<script lang="ts">
	/**
	 * A determinate progress bar.
	 *
	 * `packages/ui` has none to mirror. This replaces a native `<progress>`,
	 * which cannot be styled consistently across browsers without fighting three
	 * vendor pseudo-elements, and carries the ARIA the native element gave for
	 * free so nothing is lost in the trade.
	 */
	import { cn } from '$lib/utils';

	let {
		value = 0,
		max = 100,
		label,
		class: className
	}: {
		value?: number;
		max?: number;
		label?: string;
		class?: string;
	} = $props();

	const pct = $derived(max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0);
</script>

<div
	data-slot="progress"
	role="progressbar"
	aria-valuenow={value}
	aria-valuemin={0}
	aria-valuemax={max}
	aria-label={label}
	class={cn('bg-muted h-1.5 w-full overflow-hidden rounded-full', className)}
>
	<div
		class="bg-primary h-full rounded-full transition-[width] duration-200"
		style="width: {pct}%"
	></div>
</div>
