<script lang="ts">
	import { enhance } from '$app/forms';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { count, delta, relative, staleness } from '$lib/ops/format';

	let { data, form } = $props();

	let filter = $state('');
	let onlyProblems = $state(false);

	const rows = $derived(
		(data.sources ?? [])
			.filter((s: any) => !filter || s.source_id.includes(filter) || s.label?.toLowerCase().includes(filter.toLowerCase()))
			.filter((s: any) => !onlyProblems || problem(s))
	);

	/** Worth an operator's attention: stale, shrinking, failing, or switched off. */
	function problem(source: any): boolean {
		const change = delta(source.last_records, source.previous_records);
		return (
			!source.enabled ||
			source.paused ||
			source.last_success_at == null ||
			(source.staleness_seconds ?? 0) > 36 * 3600 ||
			(change != null && change.share < -30) ||
			source.failures_7d > 0
		);
	}
</script>

<svelte:head><title>Sources · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<div class="mb-4 flex flex-wrap items-center gap-3">
		<h1 class="text-lg font-semibold">Sources</h1>
		<input
			class="input input-bordered input-sm"
			placeholder="filter"
			bind:value={filter}
		/>
		<label class="flex items-center gap-2 text-sm">
			<input type="checkbox" class="checkbox checkbox-sm" bind:checked={onlyProblems} />
			needs attention
		</label>
		<span class="text-muted-foreground ml-auto text-sm">{rows.length} shown</span>
	</div>

	{#if form?.error}
		<div class="alert alert-error mb-4 text-sm">{form.error}</div>
	{/if}

	<p class="text-muted-foreground mb-3 text-sm">
		The staleness badge is the point of this page: a source that silently stopped returning
		records is the failure the whole pipeline exists to catch, and nothing else surfaces it.
	</p>

	<div class="overflow-x-auto">
		<table class="table table-sm bg-card rounded shadow-sm">
			<thead>
				<tr>
					<th>Source</th>
					<th>Scraper</th>
					<th>Last success</th>
					<th class="text-right">Records</th>
					<th class="text-right">Δ</th>
					<th class="text-right">7d fails</th>
					<th>Controls</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as source (source.source_id)}
					{@const stale = staleness(source.staleness_seconds)}
					{@const change = delta(source.last_records, source.previous_records)}
					<tr class="hover">
						<td>
							{#if source.last_job_id && source.last_run_id}
								<a class="link font-medium" href="/ops/runs/{source.last_run_id}/jobs/{source.last_job_id}">{source.source_id}</a>
							{:else}
								<div class="font-medium">{source.source_id}</div>
							{/if}
							<div class="text-muted-foreground/70 max-w-56 truncate text-xs">{source.label}</div>
						</td>
						<td class="text-xs">{source.scraper}</td>
						<td>
							<span class="badge badge-sm {stale.tone}" title={source.last_success_at ?? 'never'}>
								{source.last_success_at ? `${stale.label} ago` : 'never'}
							</span>
						</td>
						<td class="text-right tabular-nums">{count(source.last_records)}</td>
						<td class="text-right tabular-nums">
							{#if change}
								<span class={change.share < -30 ? 'text-destructive font-medium' : change.share < 0 ? 'text-warning' : 'opacity-60'}>
									{change.change > 0 ? '+' : ''}{count(change.change)}
									<span class="text-xs">({change.share.toFixed(0)}%)</span>
								</span>
							{:else}
								<span class="opacity-30">—</span>
							{/if}
						</td>
						<td class="text-right tabular-nums {source.failures_7d ? 'text-destructive' : 'opacity-40'}">
							{source.failures_7d}
						</td>
						<td>
							<form method="POST" action="?/update" use:enhance class="flex items-center gap-2">
								<input type="hidden" name="id" value={source.source_id} />
								<label class="flex items-center gap-1 text-xs" title="excluded from future runs when off">
									<input type="checkbox" class="toggle toggle-xs" name="enabled" checked={source.enabled} />
									enabled
								</label>
								<label class="flex items-center gap-1 text-xs" title="queued jobs are not claimed while paused">
									<input type="checkbox" class="checkbox checkbox-xs" name="paused" checked={source.paused} />
									paused
								</label>
								<select name="schedule_id" class="select select-bordered select-xs w-28">
									<option value="">default</option>
									{#each data.schedules ?? [] as schedule (schedule.id)}
										<option value={schedule.id} selected={source.schedule_id === schedule.id}>
											{schedule.id}
										</option>
									{/each}
								</select>
								<button class="btn btn-xs" type="submit">save</button>
							</form>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
