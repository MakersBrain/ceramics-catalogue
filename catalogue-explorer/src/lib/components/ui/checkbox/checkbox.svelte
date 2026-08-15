<script lang="ts" module>
	/**
	 * Exported because `bind:group` is a compiler feature of the native `<input>`
	 * element and does not cross a component boundary - Svelte has no way to
	 * marshal a group binding through props. A multi-select list therefore has to
	 * write the element itself, and this is what keeps it looking like every
	 * other checkbox instead of drifting the moment one of them is edited.
	 */
	export const checkboxClass =
		'border-input accent-primary focus-visible:ring-ring/30 size-4 shrink-0 cursor-pointer rounded-xs outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50';
</script>

<script lang="ts">
	import type { HTMLInputAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * A native checkbox, restyled with `accent-color`.
	 *
	 * `packages/ui` has no checkbox to mirror, so this is written to the same
	 * conventions rather than copied. It stays a real `<input type="checkbox">`
	 * because everything that would justify a custom control here - an
	 * indeterminate state, a styled tick, an animated transition - is absent,
	 * while everything the native one gives is wanted: the platform's own hit
	 * target, its keyboard behaviour, and the form semantics the ops pages post
	 * with.
	 *
	 * `accent-color` is the whole restyle. It is the one property that tints a
	 * native check without replacing it, and pointing it at `--primary` puts the
	 * ticked state in brand clay in both themes.
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
	data-slot="checkbox"
	class={cn(checkboxClass, className)}
	bind:checked
	{...restProps}
/>
