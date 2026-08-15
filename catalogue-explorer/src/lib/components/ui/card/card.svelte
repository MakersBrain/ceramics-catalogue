<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * A flat card: hairline border, card ground, no shadow.
	 *
	 * That is the brand's rule rather than shadcn's default, and it is worth
	 * keeping - elevation is reserved for things that genuinely float above the
	 * page, so that when a dialog does cast a shadow the shadow still means
	 * something. `packages/ui` reaches the same look through `ring-1` plus
	 * `shadow-sm`; a plain border is the same picture with one less concept.
	 *
	 * `--card-spacing` is the single number every padding derives from, which is
	 * what lets `size="sm"` retune a whole card rather than each of its parts.
	 * The steps here are tighter than the console's (1.25rem / 0.75rem against
	 * its 2rem / 1.25rem): the ops pages show fifteen cards at once, and a card
	 * built for a page of prose wastes most of that screen.
	 */
	let {
		ref = $bindable(null),
		class: className,
		children,
		size = 'default',
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> & { size?: 'default' | 'sm' } = $props();
</script>

<div
	bind:this={ref}
	data-slot="card"
	data-size={size}
	class={cn(
		'bg-card text-card-foreground group/card flex flex-col gap-(--card-spacing) overflow-hidden rounded-lg border py-(--card-spacing) text-sm [--card-spacing:--spacing(5)] data-[size=sm]:[--card-spacing:--spacing(3)]',
		className
	)}
	{...restProps}
>
	{@render children?.()}
</div>
