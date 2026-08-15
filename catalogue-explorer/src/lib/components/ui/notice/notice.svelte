<script lang="ts">
	/**
	 * An inline message attached to the thing that produced it, rather than a
	 * toast. Most of this app is a control that either changed something or
	 * refused to, and the answer belongs next to the control: a toast that has
	 * faded is an answer nobody can re-read.
	 *
	 * `role` follows the kind rather than being a prop, and the distinction is
	 * not cosmetic: `alert` interrupts a screen reader and `status` waits for a
	 * pause. A refusal is worth interrupting for; a confirmation is not.
	 *
	 * The text stays the normal reading colour and the border carries the
	 * severity, which is the brand's rule and the reason these are legible at
	 * all - a tinted paragraph on a tinted ground is not.
	 */
	import type { Snippet } from 'svelte';
	import { cn } from '$lib/utils';

	let {
		kind = 'info',
		class: className,
		children
	}: {
		kind?: 'info' | 'error' | 'success' | 'warning';
		class?: string;
		children?: Snippet;
	} = $props();

	const styles: Record<string, string> = {
		info: 'border-border bg-muted',
		error: 'border-destructive/40 bg-destructive/10',
		success: 'border-success/40 bg-success/10',
		warning: 'border-warning/40 bg-warning/10'
	};
</script>

<div
	data-slot="notice"
	role={kind === 'error' ? 'alert' : 'status'}
	class={cn('text-foreground rounded-md border px-3 py-2 text-sm', styles[kind], className)}
>
	{@render children?.()}
</div>
