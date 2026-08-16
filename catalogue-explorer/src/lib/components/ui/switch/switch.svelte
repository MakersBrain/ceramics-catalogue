<script lang="ts">
	import type { HTMLInputAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * A native checkbox presented as an on/off switch.
	 *
	 * Keeping the native input preserves keyboard and form behaviour while the
	 * track and thumb make a persistent setting read as a state, not a one-shot
	 * action.
	 */
	let {
		ref = $bindable(null),
		checked = $bindable(),
		class: className,
		...restProps
	}: WithElementRef<HTMLInputAttributes, HTMLInputElement> = $props();
</script>

<input
	bind:this={ref}
	type="checkbox"
	role="switch"
	data-slot="switch"
	class={cn(
		'border-input bg-muted focus-visible:ring-ring/30 relative h-5 w-9 shrink-0 cursor-pointer appearance-none rounded-full border outline-none transition-colors after:absolute after:top-0.5 after:left-0.5 after:size-3.5 after:rounded-full after:bg-white after:shadow-sm after:transition-transform checked:border-primary checked:bg-primary checked:after:translate-x-4 focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50',
		className
	)}
	bind:checked
	{...restProps}
/>
