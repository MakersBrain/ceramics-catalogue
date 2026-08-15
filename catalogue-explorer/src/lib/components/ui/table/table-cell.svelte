<script lang="ts">
	import type { HTMLTdAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLTdAttributes, HTMLTableCellElement> = $props();
</script>

<td
	bind:this={ref}
	data-slot="table-cell"
	class={cn('px-3 py-2 align-middle [&:has([role=checkbox])]:pr-0', className)}
	{...restProps}
>
	{@render children?.()}
</td>
