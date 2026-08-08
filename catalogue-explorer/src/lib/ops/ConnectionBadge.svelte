<script lang="ts">
	import type { ConnectionState } from './stream.svelte';

	// An operations page that has quietly stopped updating is worse than one
	// that admits it, so this is always visible rather than only on failure.
	let { state }: { state: ConnectionState } = $props();

	const look = $derived(
		{
			live: { dot: 'bg-success', text: 'live', title: 'receiving events' },
			connecting: { dot: 'bg-warning animate-pulse', text: 'connecting', title: 'opening the stream' },
			reconnecting: {
				dot: 'bg-warning animate-pulse',
				text: 'reconnecting',
				title: 'the stream dropped; polling every 5s until it returns'
			},
			offline: { dot: 'bg-error', text: 'offline', title: 'no stream; polling every 5s' }
		}[state]
	);
</script>

<span class="flex items-center gap-1.5" title={look.title}>
	<span class="inline-block h-2 w-2 rounded-full {look.dot}"></span>
	<span class="text-base-content/60 text-xs">{look.text}</span>
</span>
