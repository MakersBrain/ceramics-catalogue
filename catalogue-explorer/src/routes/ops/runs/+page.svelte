<script lang="ts">
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';

	let { data } = $props();
</script>

<svelte:head><title>Runs · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<h1 class="mb-4 text-lg font-semibold">Run history</h1>

	<div class="overflow-x-auto">
		<table class="table-zebra table table-sm bg-base-100 rounded shadow-sm">
			<thead>
				<tr>
					<th>Started</th>
					<th>Kind</th>
					<th>Requested by</th>
					<th>Status</th>
					<th class="text-right">ok</th>
					<th class="text-right">failed</th>
					<th class="text-right">records</th>
					<th>Duration</th>
				</tr>
			</thead>
			<tbody>
				{#each data.runs ?? [] as run (run.id)}
					<tr class="hover">
						<td>
							<a class="link" href="/ops/runs/{run.id}" title={run.created_at}>
								{relative(run.created_at)}
							</a>
						</td>
						<td>{run.kind}</td>
						<td class="text-base-content/60 max-w-40 truncate">{run.requested_by ?? '—'}</td>
						<td><span class="badge badge-sm {stateTone(run.status)}">{run.status}</span></td>
						<td class="text-right">{run.succeeded}</td>
						<td class="text-right {run.failed ? 'text-error' : ''}">{run.failed}</td>
						<td class="text-right">{count(run.summary?.records)}</td>
						<td>{duration(run.started_at, run.finished_at)}</td>
					</tr>
				{:else}
					<tr>
						<td colspan="8" class="text-base-content/60">
							No runs recorded yet. Start one from the overview, or run
							<code>catalogue-dump --record</code>.
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	{#if data.next_cursor}
		<a class="btn btn-sm mt-4" href="/ops/runs?cursor={encodeURIComponent(data.next_cursor)}">
			Older runs
		</a>
	{/if}
{/if}
