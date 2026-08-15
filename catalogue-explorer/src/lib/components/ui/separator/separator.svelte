<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	let {
		ref = $bindable(null),
		class: className,
		orientation = 'horizontal',
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> & {
		orientation?: 'horizontal' | 'vertical';
	} = $props();
</script>

<div
	bind:this={ref}
	data-slot="separator"
	role="separator"
	aria-orientation={orientation}
	class={cn(
		'bg-border shrink-0',
		orientation === 'horizontal' ? 'h-px w-full' : 'h-full w-px',
		className
	)}
	{...restProps}
></div>
