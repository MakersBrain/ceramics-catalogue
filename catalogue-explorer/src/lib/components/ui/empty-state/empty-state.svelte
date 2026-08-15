<script lang="ts">
	/**
	 * One of the three terminal states of a view, styled so that none of them is
	 * a blank screen the reader has to interpret. An empty table and a broken
	 * query look identical otherwise.
	 */
	import type { Snippet } from 'svelte';
	import { cn } from '$lib/utils';

	let {
		title,
		description,
		action,
		class: className
	}: {
		title: string;
		description?: string;
		action?: Snippet;
		class?: string;
	} = $props();
</script>

<section
	data-slot="empty-state"
	class={cn('rounded-lg border border-dashed px-4 py-10 text-center', className)}
>
	<h2 class="font-sans text-sm font-semibold">{title}</h2>
	{#if description}
		<p class="text-muted-foreground mx-auto mt-1 max-w-prose text-sm">{description}</p>
	{/if}
	{#if action}
		<div class="mt-4 flex justify-center">{@render action()}</div>
	{/if}
</section>
