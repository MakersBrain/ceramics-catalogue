<script lang="ts">
	import { page } from '$app/state';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';

	let { data } = $props();

	const job = $derived(data.job);
	const coverage = $derived(Object.entries(job?.summary?.field_coverage ?? {}) as [string, number][]);
	const errors = $derived((job?.summary?.errors ?? []) as { url: string; error: string }[]);

	const levelTone: Record<string, string> = {
		error: 'text-error',
		warning: 'text-warning',
		info: '',
		debug: 'text-base-content/40'
	};
</script>

<svelte:head><title>{job?.source_id ?? 'Job'} · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else if job}
	<div class="mb-4 flex flex-wrap items-baseline gap-3">
		<a class="link text-sm" href="/ops/runs/{page.params.id}">← run</a>
		<h1 class="text-lg font-semibold">{job.source_id}</h1>
		<span class="badge {stateTone(job.state)}">{job.state}</span>
		<span class="text-base-content/60 text-sm">
			attempt {job.attempt}/{job.max_attempts} · {duration(job.started_at, job.finished_at)}
			{#if job.finished_at}· finished {relative(job.finished_at)}{/if}
		</span>
	</div>

	<div class="mb-6 grid gap-4 lg:grid-cols-3">
		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4 text-sm">
				<h2 class="text-base-content/60 text-xs uppercase">Collection</h2>
				<dl class="grid grid-cols-2 gap-x-3 gap-y-1">
					<dt class="text-base-content/60">records</dt>
					<dd class="tabular-nums">{count(job.records ?? job.summary?.records)}</dd>
					<dt class="text-base-content/60">requests</dt>
					<dd class="tabular-nums">{count(job.requests ?? job.summary?.requests)}</dd>
					<dt class="text-base-content/60">rendered</dt>
					<dd class="tabular-nums">{count(job.rendered_pages ?? job.summary?.rendered_pages)}</dd>
					<dt class="text-base-content/60">errors</dt>
					<dd class="tabular-nums">{count(job.error_count ?? job.summary?.error_count)}</dd>
					<dt class="text-base-content/60">truncated</dt>
					<dd>{job.summary?.truncated ? 'yes' : 'no'}</dd>
				</dl>
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4 text-sm">
				<h2 class="text-base-content/60 text-xs uppercase">Artifact</h2>
				{#if job.artifact_path}
					<p class="break-all font-mono text-xs">{job.artifact_path}</p>
					<p class="text-base-content/60 text-xs">
						{count(job.artifact_size)} bytes
					</p>
					<p class="text-base-content/40 break-all font-mono text-xs" title="sha256">
						{job.artifact_sha256?.slice(0, 32)}…
					</p>
				{:else}
					<p class="text-base-content/50">No artifact recorded for this attempt.</p>
				{/if}
				{#if job.trace_id}
					<p class="text-base-content/40 mt-2 font-mono text-xs">trace {job.trace_id}</p>
				{/if}
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4 text-sm">
				<h2 class="text-base-content/60 text-xs uppercase">In flight</h2>
				{#if (job.in_flight ?? []).length}
					<ul class="space-y-1">
						{#each job.in_flight as request (request.url)}
							<li class="truncate font-mono text-xs" title={request.url}>
								<span class="text-base-content/50">{request.seconds}s</span>
								{request.url}
							</li>
						{/each}
					</ul>
				{:else}
					<p class="text-base-content/50">Nothing in flight.</p>
				{/if}
			</div>
		</div>
	</div>

	{#if coverage.length}
		<section class="mb-6">
			<h2 class="mb-2 text-sm font-semibold uppercase opacity-60">
				Field coverage
				<span class="ml-1 font-normal normal-case opacity-70">
					— rows carrying each field, so a thin scraper is visible
				</span>
			</h2>
			<div class="card bg-base-100 shadow-sm">
				<div class="card-body grid gap-1 p-4 sm:grid-cols-2 lg:grid-cols-3">
					{#each coverage.sort((a, b) => b[1] - a[1]) as [field, rows] (field)}
						{@const share = job.summary?.records ? (100 * rows) / job.summary.records : 0}
						<div class="flex items-center gap-2 text-xs">
							<span class="w-40 truncate">{field}</span>
							<progress class="progress h-1.5 flex-1" value={share} max="100"></progress>
							<span class="tabular-nums w-10 text-right opacity-60">{share.toFixed(0)}%</span>
						</div>
					{/each}
				</div>
			</div>
		</section>
	{/if}

	{#if errors.length}
		<section class="mb-6">
			<h2 class="mb-2 text-sm font-semibold uppercase opacity-60">Errors</h2>
			<div class="card bg-base-100 shadow-sm">
				<ul class="card-body gap-2 p-4 text-xs">
					{#each errors as entry (entry.url + entry.error)}
						<li>
							<div class="truncate font-mono opacity-60" title={entry.url}>{entry.url}</div>
							<div class="text-error">{entry.error}</div>
						</li>
					{/each}
				</ul>
			</div>
		</section>
	{/if}

	<section>
		<form method="GET" class="mb-2 flex flex-wrap items-center gap-2">
			<h2 class="mr-2 text-sm font-semibold uppercase opacity-60">Log</h2>
			<select name="level" class="select select-bordered select-xs" value={data.level ?? ''}>
				<option value="">all levels</option>
				<option value="error">error</option>
				<option value="warning">warning</option>
				<option value="info">info</option>
				<option value="debug">debug</option>
			</select>
			<input
				name="q"
				class="input input-bordered input-xs"
				placeholder="search"
				value={data.search ?? ''}
			/>
			<button class="btn btn-xs" type="submit">Filter</button>
		</form>

		<div class="bg-base-100 max-h-[32rem] overflow-y-auto rounded p-3 font-mono text-xs shadow-sm">
			{#each data.lines ?? [] as line (line.id)}
				<div class="flex gap-2 {levelTone[line.level] ?? ''}">
					<span class="opacity-40">{new Date(line.at).toLocaleTimeString('en-GB')}</span>
					<span class="w-16 shrink-0 opacity-60">{line.event ?? line.level}</span>
					<span class="break-all">{line.message}</span>
				</div>
			{:else}
				<p class="text-base-content/50">
					No log lines. Lines are written at info and above; start the run with
					<code>log_level=debug</code> for more.
				</p>
			{/each}
		</div>
	</section>
{/if}
