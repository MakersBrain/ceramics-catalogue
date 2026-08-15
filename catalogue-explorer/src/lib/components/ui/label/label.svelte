<script lang="ts">
	import type { HTMLLabelAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLLabelAttributes, HTMLLabelElement> = $props();
</script>

<label
	bind:this={ref}
	data-slot="label"
	class={cn(
		'text-muted-foreground flex items-center gap-1.5 text-[0.6875rem] font-semibold tracking-[0.08em] uppercase select-none',
		className
	)}
	{...restProps}
>
	{@render children?.()}
</label>
