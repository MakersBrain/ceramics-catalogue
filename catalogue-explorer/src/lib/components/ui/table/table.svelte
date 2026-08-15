<script lang="ts">
	import type { HTMLTableAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * The wrapper owns the horizontal scroll, not the page.
	 *
	 * Fifteen-column tables are normal here, and a table that widens its parent
	 * takes the whole layout with it - the nav included. Scrolling inside the
	 * frame keeps the overflow where the reader expects to find it.
	 */
	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLTableAttributes, HTMLTableElement> = $props();
</script>

<div data-slot="table-container" class="relative w-full overflow-x-auto">
	<table
		bind:this={ref}
		data-slot="table"
		class={cn('w-full caption-bottom text-sm', className)}
		{...restProps}
	>
		{@render children?.()}
	</table>
</div>
