<script lang="ts">
	import { page } from '$app/state';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';

	let { data } = $props();

	const job = $derived(data.job);
	const coverage = $derived(Object.entries(job?.summary?.field_coverage ?? {}) as [string, number][]);
	const errors = $derived((job?.summary?.errors ?? []) as { url: string; error: string }[]);
	const changes = $derived(data.changes);
	const changeItems = $derived(changes?.items ?? []);

	const levelTone: Record<string, string> = {
		error: 'text-error',
		warning: 'text-warning',
		info: '',
		debug: 'text-base-content/40'
	};

	function value(value: unknown): string {
		if (value == null) return '—';
		if (typeof value === 'string') return value;
		return JSON.stringify(value);
	}
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

	<section class="mb-6">
		<div class="mb-2 flex flex-wrap items-baseline gap-2">
			<h2 class="text-sm font-semibold uppercase opacity-60">Changes since previous scrape</h2>
			{#if changes}
				<a
					class="link text-xs"
					href="/ops/runs/{changes.previous_run_id}/jobs/{changes.previous_job_id}"
				>
					previous artifact · {relative(changes.previous_finished_at)}
				</a>
			{/if}
		</div>

		{#if changes}
			<div class="stats stats-horizontal bg-base-100 mb-3 shadow-sm">
				<div class="stat px-4 py-2">
					<div class="stat-title text-xs">Added</div><div class="stat-value text-success text-lg">{count(changes.added)}</div>
				</div>
				<div class="stat px-4 py-2">
					<div class="stat-title text-xs">Removed</div><div class="stat-value text-error text-lg">{count(changes.removed)}</div>
				</div>
				<div class="stat px-4 py-2">
					<div class="stat-title text-xs">Changed</div><div class="stat-value text-warning text-lg">{count(changes.changed)}</div>
				</div>
				<div class="stat px-4 py-2">
					<div class="stat-title text-xs">Unchanged</div><div class="stat-value text-lg">{count(changes.unchanged)}</div>
				</div>
			</div>

			<form method="GET" class="mb-2 flex flex-wrap items-center gap-2">
				<select name="change_kind" class="select select-bordered select-xs" value={data.changeKind ?? ''}>
					<option value="">all changes</option>
					<option value="added">added</option>
					<option value="removed">removed</option>
					<option value="changed">changed</option>
				</select>
				<input
					name="change_q"
					class="input input-bordered input-xs"
					placeholder="product name or id"
					value={data.changeSearch ?? ''}
				/>
				<button class="btn btn-xs" type="submit">Filter</button>
				<span class="text-base-content/50 text-xs">
					{count(changes.matched)} matching{(changes.matched ?? 0) > changeItems.length ? ` · showing first ${changeItems.length}` : ''}
				</span>
			</form>

			<div class="overflow-x-auto rounded bg-base-100 shadow-sm">
				<table class="table table-sm">
					<thead><tr><th>Change</th><th>Product</th><th>Fields</th></tr></thead>
					<tbody>
						{#each changeItems as change (change.kind + change.external_id)}
							{@const fields = change.fields ?? []}
							<tr class="align-top">
								<td>
									<span class="badge badge-sm {change.kind === 'added' ? 'badge-success' : change.kind === 'removed' ? 'badge-error' : 'badge-warning'}">
										{change.kind}
									</span>
								</td>
								<td>
									<div>{change.name ?? 'unnamed record'}</div>
									<div class="text-base-content/40 break-all font-mono text-xs">{change.external_id}</div>
								</td>
								<td class="min-w-80">
									{#if fields.length}
										<dl class="space-y-1 text-xs">
											{#each fields as field (field.field)}
												<div class="grid grid-cols-[8rem_1fr] gap-2">
													<dt class="font-medium">{field.field}</dt>
													<dd class="min-w-0 break-all">
														<span class="text-error line-through">{value(field.before)}</span>
														<span class="mx-1 opacity-40">→</span>
														<span class="text-success">{value(field.after)}</span>
													</dd>
												</div>
											{/each}
										</dl>
									{:else}
										<span class="text-base-content/40 text-xs">whole record</span>
									{/if}
								</td>
							</tr>
						{:else}
							<tr><td colspan="3" class="text-base-content/50">No matching changes.</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="bg-base-100 text-base-content/50 rounded p-4 text-sm shadow-sm">
				{data.changesUnavailable ?? 'Comparison is not available yet.'}
			</div>
		{/if}
	</section>

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
