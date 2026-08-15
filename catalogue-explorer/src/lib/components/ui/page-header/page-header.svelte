<script lang="ts">
	/**
	 * The page's identity and its actions, in one row.
	 *
	 * The heading carries `data-page-identity` and a negative tabindex for the
	 * same reason it does in the console: a route has exactly one answer to what
	 * it is, and something has to be focusable after a client-side navigation or
	 * a screen reader is left where the last page put it.
	 *
	 * Props rather than the console's `PageDescriptor`, because that type lives
	 * in its `patterns.ts` alongside its routing table and importing it would
	 * mean importing a navigation model this app does not have.
	 */
	import type { Snippet } from 'svelte';
	import { cn } from '$lib/utils';

	let {
		title,
		description,
		eyebrow,
		backHref,
		backLabel,
		actions,
		class: className
	}: {
		title: string;
		description?: string;
		eyebrow?: string;
		backHref?: string;
		backLabel?: string;
		actions?: Snippet;
		class?: string;
	} = $props();
</script>

<header
	data-slot="page-header"
	class={cn('flex flex-wrap items-start justify-between gap-4', className)}
>
	<div class="min-w-0">
		{#if backHref && backLabel}
			<a
				class="text-muted-foreground text-sm underline-offset-4 hover:underline"
				href={backHref}>{backLabel}</a
			>
		{/if}
		{#if eyebrow}
			<span
				class="text-accent-foreground mb-1 block text-[0.6875rem] font-semibold tracking-[0.08em] uppercase"
				>{eyebrow}</span
			>
		{/if}
		<h1 tabindex="-1" data-page-identity class="text-xl font-semibold tracking-tight">
			{title}
		</h1>
		{#if description}
			<p class="text-muted-foreground measure mt-1 text-sm">{description}</p>
		{/if}
	</div>
	{#if actions}
		<div class="flex flex-wrap items-center gap-2">{@render actions()}</div>
	{/if}
</header>
