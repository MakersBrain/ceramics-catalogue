<script lang="ts">
	import type { HTMLSelectAttributes } from 'svelte/elements';
	import ChevronDown from '@lucide/svelte/icons/chevron-down';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * A real `<select>`, wearing the same border as `Input`.
	 *
	 * DELIBERATELY NOT a bits-ui listbox. That is the right control when options
	 * need marks, groups, descriptions or a search; this is the right control for
	 * the fifteen places here that offer a short closed list - a source, a run
	 * state, a proxy pool - and would gain nothing from a custom popup while
	 * losing the platform's own behaviour: the native wheel on a phone,
	 * type-ahead on a keyboard, and a control every screen reader already knows.
	 *
	 * `appearance-none` plus an absolutely positioned chevron because the native
	 * arrow cannot be restyled. `pr-7` reserves its column and
	 * `pointer-events-none` on the mark keeps the click going to the select
	 * underneath it. The `color-scheme` handling that stops a dark theme opening
	 * a white option list lives in `app.css`, keyed on this `data-slot`.
	 */
	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLSelectAttributes, HTMLSelectElement> = $props();
</script>

<div class="relative inline-grid w-full max-w-full min-w-0" style="contain: inline-size;">
	<select
		bind:this={ref}
		data-slot="native-select"
		class={cn(
			'border-input bg-card text-foreground focus-visible:border-ring focus-visible:ring-ring/30 h-9 w-0 max-w-full min-w-full appearance-none rounded-md border py-1 pr-7 pl-2.5 text-sm transition-[color,border-color,box-shadow] outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
			className
		)}
		bind:value
		{...restProps}
	>
		{@render children?.()}
	</select>
	<ChevronDown
		class="text-muted-foreground pointer-events-none absolute inset-y-0 right-2 my-auto size-4"
		aria-hidden="true"
	/>
</div>
