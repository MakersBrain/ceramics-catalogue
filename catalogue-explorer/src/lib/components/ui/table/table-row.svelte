<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * Hairline rows and no zebra striping.
	 *
	 * The brand rejects stripes outright, and the reason holds here: a stripe
	 * stops working the moment a row needs a status tint of its own, which in an
	 * ops table is most of the time.
	 */
	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLTableRowElement>, HTMLTableRowElement> = $props();
</script>

<tr
	bind:this={ref}
	data-slot="table-row"
	class={cn('hover:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors', className)}
	{...restProps}
>
	{@render children?.()}
</tr>
