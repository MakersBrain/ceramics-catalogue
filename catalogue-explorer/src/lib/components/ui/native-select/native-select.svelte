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
	 *
	 * WIDTH GOES ON `wrapperClass`, NOT `class`. The select is pinned to
	 * `w-0 min-w-full` so that a long option cannot blow the control out past
	 * its column - that pinning, with `contain: inline-size`, is the whole
	 * reason this element is wrapped at all. It also means a width passed
	 * through `class` lands on the wrong box and loses: `w-auto` on the select
	 * still has `min-w-full` holding it to the wrapper, and the wrapper is
	 * still `w-full`. In a flex row that collapsed the control to a stub and
	 * pushed the submit button through it.
	 *
	 * TWO SIZING MODES, BECAUSE THE PINNING AND CONTENT-SIZING CANNOT COEXIST.
	 * Filling its column is the default and the right behaviour in a table cell
	 * or a form grid, where a long option must not widen the layout. But it
	 * needs a definite width to fill, so it cannot also shrink to its content:
	 * an auto-width wrapper around a `w-0` select computes to zero, which is
	 * the stub described above and exactly what a first attempt at fixing this
	 * by passing `w-auto` produced.
	 *
	 * `fit` is that other mode. It drops the pinning so the select takes its
	 * natural width from the longest option, which is what a filter sitting
	 * inline in a toolbar wants. Use it there; leave it off inside a grid.
	 * Fixed widths go on `wrapperClass`, and height and type on `class`.
	 */
	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		wrapperClass = undefined,
		fit = false,
		children,
		...restProps
	}: WithElementRef<HTMLSelectAttributes, HTMLSelectElement> & {
		wrapperClass?: string;
		fit?: boolean;
	} = $props();
</script>

<div
	class={cn('relative inline-grid max-w-full min-w-0', fit ? 'w-auto' : 'w-full', wrapperClass)}
	style={fit ? undefined : 'contain: inline-size;'}
>
	<select
		bind:this={ref}
		data-slot="native-select"
		class={cn(
			'border-input bg-card text-foreground focus-visible:border-ring focus-visible:ring-ring/30 h-9 max-w-full appearance-none rounded-md border py-1 pr-7 pl-2.5 text-sm transition-[color,border-color,box-shadow] outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
			fit ? 'w-auto' : 'w-0 min-w-full',
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
