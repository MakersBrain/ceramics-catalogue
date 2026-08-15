<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * Bitter, because it is a heading. Not uppercase: the brand spends that
	 * treatment on micro labels, and a card title is not one.
	 */
	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> = $props();
</script>

<div
	bind:this={ref}
	data-slot="card-title"
	class={cn('font-heading text-base leading-snug font-semibold', className)}
	{...restProps}
>
	{@render children?.()}
</div>
