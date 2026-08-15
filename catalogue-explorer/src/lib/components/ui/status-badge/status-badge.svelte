<script lang="ts">
	/**
	 * A badge that says how something is going, not what kind of thing it is.
	 *
	 * SEPARATE FROM `Badge` BECAUSE THE AXIS IS DIFFERENT. That one varies by
	 * emphasis and is the right control for labelling a category; this one varies
	 * by outcome and is the right control for a run that failed, a proxy that is
	 * live, a source that is stale. Folding them together would mean a caller
	 * picking "secondary" when it means "fine", which is how a status ends up
	 * styled by whoever wrote the row.
	 *
	 * The tones resolve to this app's validated status trio rather than the
	 * brand's sage/kiln/danger - see the note in `app.css`. `warn` is violet, not
	 * amber, and that is the whole point of it: against `good` this green, amber
	 * separates by dE 2.4 under deuteranopia.
	 *
	 * COLOUR IS NEVER THE ONLY CARRIER. Every call site puts a word inside, so a
	 * greyscale reader loses the emphasis and keeps the meaning.
	 */
	import type { Snippet } from 'svelte';
	import { cn } from '$lib/utils';

	let {
		tone = 'neutral',
		class: className,
		children
	}: {
		tone?: 'neutral' | 'good' | 'bad' | 'warn' | 'busy';
		class?: string;
		children?: Snippet;
	} = $props();

	/**
	 * `busy` is the one tone that reads from the brand rather than the validated
	 * trio, and it is not an exception to the rule above: "a run is in progress"
	 * is not an outcome, so it has no business borrowing a colour that means
	 * one. The brand names this exact case - `.badge.busy`, accent tint under
	 * accent text - and clay against the other four is unambiguous in all three
	 * simulations, because it is the only warm one among them.
	 */
	const tones: Record<string, string> = {
		neutral: 'bg-muted text-muted-foreground',
		good: 'bg-success/15 text-success',
		bad: 'bg-destructive/15 text-destructive',
		warn: 'bg-warning/15 text-warning',
		busy: 'bg-accent text-accent-foreground'
	};
</script>

<span
	data-slot="status-badge"
	class={cn(
		'inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-semibold whitespace-nowrap',
		tones[tone],
		className
	)}
>
	{@render children?.()}
</span>
