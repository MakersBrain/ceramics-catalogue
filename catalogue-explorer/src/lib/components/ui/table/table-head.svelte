<script lang="ts">
	import type { HTMLThAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * Uppercase, tracked, micro. This is one of the two places the brand spends
	 * that treatment - the other being the eyebrow - and it is why the buttons
	 * deliberately do not use it.
	 */
	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLThAttributes, HTMLTableCellElement> = $props();
</script>

<th
	bind:this={ref}
	data-slot="table-head"
	class={cn(
		'text-muted-foreground h-9 px-3 text-left align-middle text-[0.6875rem] font-semibold tracking-[0.08em] whitespace-nowrap uppercase [&:has([role=checkbox])]:pr-0',
		className
	)}
	{...restProps}
>
	{@render children?.()}
</th>
