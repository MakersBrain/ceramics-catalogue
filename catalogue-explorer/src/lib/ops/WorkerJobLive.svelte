<script lang="ts">
	import type { WorkerJob } from './types';
	import type { OpsStream } from './stream.svelte';
	import { count, relative } from './format';
	import WorkerTrace from './WorkerTrace.svelte';

	let { job, stream }: { job: WorkerJob; stream: OpsStream } = $props();
	const progress = $derived(stream.progress[job.job_id]);
	const rate = $derived(stream.jobRate(job.job_id));
	let traceOpen = $state(false);
</script>

<div class="py-2">
	<div class="flex items-center justify-between gap-2 text-xs">
		<a
			class="text-accent-foreground min-w-0 truncate font-medium underline-offset-4 hover:underline"
			href="/ops/runs/{job.run_id}/jobs/{job.job_id}"
		>
			{job.source}
		</a>
		<span class="text-muted-foreground shrink-0">
			{progress?.phase ?? 'starting'}{progress ? ` · ${relative(progress.at)}` : ''}
		</span>
	</div>

	{#if progress}
		<dl class="mt-2 grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
			<div><dt class="text-muted-foreground">indexed</dt><dd class="tabular-nums font-medium">{count(progress.records)}</dd></div>
			<div><dt class="text-muted-foreground">discovered</dt><dd class="tabular-nums font-medium">{count(progress.discovered)}</dd></div>
			<div><dt class="text-muted-foreground">errors</dt><dd class="tabular-nums font-medium {progress.errors ? 'text-destructive' : ''}">{count(progress.errors)}</dd></div>
			<div><dt class="text-muted-foreground">requests</dt><dd class="tabular-nums">{count(progress.requests)}</dd></div>
			<div><dt class="text-muted-foreground">index rate</dt><dd class="tabular-nums">{rate ? `${rate.records.toFixed(1)}/s` : '—'}</dd></div>
			<div><dt class="text-muted-foreground">request rate</dt><dd class="tabular-nums">{rate ? `${rate.requests.toFixed(1)}/s` : '—'}</dd></div>
		</dl>
		{#if progress.in_flight?.length}
			<div class="text-muted-foreground mt-2 truncate font-mono text-[0.68rem]" title={progress.in_flight[0].url}>
				{progress.in_flight.length} in flight · {progress.in_flight[0].url}
			</div>
		{/if}
	{/if}

	<details class="mt-2" bind:open={traceOpen}>
		<summary class="cursor-pointer text-xs">Live log trace</summary>
		{#if traceOpen}<WorkerTrace jobId={job.job_id} />{/if}
	</details>
</div>
