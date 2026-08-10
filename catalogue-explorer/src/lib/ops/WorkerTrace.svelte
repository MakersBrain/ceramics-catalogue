<script lang="ts">
	import { onMount } from 'svelte';
	import type { LogLine, LogPage } from './types';

	let { jobId }: { jobId: string } = $props();
	let lines = $state<LogLine[]>([]);
	let after = $state(0);
	let error = $state<string | null>(null);

	const tones: Record<string, string> = {
		error: 'text-error',
		warning: 'text-warning',
		info: '',
		debug: 'text-base-content/40'
	};

	async function refresh() {
		try {
			const response = await fetch(`/ops/jobs/${encodeURIComponent(jobId)}/logs?after=${after}&limit=100`);
			if (!response.ok) throw new Error(`log request failed (${response.status})`);
			const page = (await response.json()) as LogPage;
			if (page.lines.length) {
				lines = [...lines, ...page.lines].slice(-100);
				after = page.next_after ?? page.lines.at(-1)?.id ?? after;
			}
			error = null;
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		}
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => void refresh(), 2000);
		return () => clearInterval(timer);
	});
</script>

<div class="bg-base-200 mt-2 max-h-52 overflow-y-auto rounded p-2 font-mono text-[0.68rem]">
	{#each lines as line (line.id)}
		<div class="grid grid-cols-[3.8rem_4.5rem_1fr] gap-1 {tones[line.level] ?? ''}">
			<span class="opacity-40">{new Date(line.at).toLocaleTimeString('en-GB')}</span>
			<span class="truncate opacity-60" title={line.event ?? line.level}>{line.event ?? line.level}</span>
			<span class="min-w-0 break-words">{line.message}</span>
		</div>
	{:else}
		<p class="text-base-content/50">Waiting for log lines…</p>
	{/each}
	{#if error}<p class="text-error mt-1">{error}</p>{/if}
</div>
