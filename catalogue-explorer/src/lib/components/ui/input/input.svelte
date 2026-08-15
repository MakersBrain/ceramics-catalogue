<script lang="ts">
	import type { HTMLInputAttributes, HTMLInputTypeAttribute } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	/**
	 * A bordered field, NOT the underline `packages/ui` uses.
	 *
	 * That is a considered departure from the sera style rather than an
	 * oversight, on two grounds. The brand specifies the control directly - a
	 * full rule at `--mb-line-strong`, measured at 3.33:1 on the page ground
	 * precisely so a control announces itself - and this app puts twenty-five
	 * controls in a single ops toolbar, where an underline field is genuinely
	 * hard to find and a boxed one is not. The console's forms are a column of
	 * five labelled rows; the underline is right there and wrong here.
	 *
	 * `h-9` rather than the console's `h-10` for the same density reason. Call
	 * sites that need tighter pass `class="h-7 text-xs"`, which `cn` resolves in
	 * the caller's favour.
	 */
	type InputType = Exclude<HTMLInputTypeAttribute, 'file'>;

	type Props = WithElementRef<
		Omit<HTMLInputAttributes, 'type'> &
			({ type: 'file'; files?: FileList } | { type?: InputType; files?: undefined })
	>;

	let {
		ref = $bindable(null),
		value = $bindable(),
		type,
		files = $bindable(),
		class: className,
		...restProps
	}: Props = $props();

	const base =
		'border-input bg-card text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/30 aria-invalid:border-destructive aria-invalid:ring-destructive/20 h-9 w-full min-w-0 rounded-md border px-2.5 py-1 text-sm transition-[color,border-color,box-shadow] outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 file:inline-flex file:border-0 file:bg-transparent file:text-sm file:font-medium';
</script>

{#if type === 'file'}
	<input
		bind:this={ref}
		data-slot="input"
		class={cn(base, className)}
		type="file"
		bind:files
		bind:value
		{...restProps}
	/>
{:else}
	<input
		bind:this={ref}
		data-slot="input"
		class={cn(base, className)}
		{type}
		bind:value
		{...restProps}
	/>
{/if}
