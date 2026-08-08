<script lang="ts">
	import { getContext } from 'svelte';
	import { enhance } from '$app/forms';
	import type { OpsStream } from '$lib/ops/stream.svelte';
	import WorkerCard from '$lib/ops/WorkerCard.svelte';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration } from '$lib/ops/format';

	let { data, form } = $props();
	const stream = getContext<OpsStream>('ops-stream');

	let picking = $state(false);
	let chosen = $state<string[]>([]);

	// The stream's roster is authoritative once it arrives; the loaded list is
	// only there so the first paint is not empty.
	const workers = $derived(stream.workers.length ? stream.workers : (data.workers ?? []));
	const queued = $derived(stream.queue.queued ?? 0);
	const running = $derived(stream.queue.running ?? 0);
	const lastRun = $derived((data.runs ?? [])[0]);
	const nextFire = $derived(
		(data.schedules ?? []).filter((s: any) => s.enabled && s.next_fire_at).sort((a: any, b: any) =>
			a.next_fire_at.localeCompare(b.next_fire_at)
		)[0]
	);
</script>

<svelte:head><title>Operations · catalogue</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<div class="grid gap-6">
		<section class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
			<div class="card bg-base-100 shadow-sm">
				<div class="card-body p-4">
					<div class="text-base-content/60 text-xs uppercase">Queue</div>
					<div class="text-2xl font-semibold">{queued}</div>
					<div class="text-base-content/60 text-xs">{running} running</div>
				</div>
			</div>
			<div class="card bg-base-100 shadow-sm">
				<div class="card-body p-4">
					<div class="text-base-content/60 text-xs uppercase">Last run</div>
					{#if lastRun}
						<div class="text-2xl font-semibold">{lastRun.status}</div>
						<div class="text-base-content/60 text-xs">
							{relative(lastRun.created_at)} · {lastRun.succeeded}/{lastRun.jobs} ok
						</div>
					{:else}
						<div class="text-base-content/40 text-2xl font-semibold">—</div>
					{/if}
				</div>
			</div>
			<div class="card bg-base-100 shadow-sm">
				<div class="card-body p-4">
					<div class="text-base-content/60 text-xs uppercase">Next scheduled</div>
					<div class="text-2xl font-semibold">
						{nextFire ? relative(nextFire.next_fire_at) : '—'}
					</div>
					<div class="text-base-content/60 text-xs">{nextFire?.id ?? 'no schedule enabled'}</div>
				</div>
			</div>
			<div class="card bg-base-100 shadow-sm">
				<div class="card-body p-4">
					<div class="text-base-content/60 text-xs uppercase">Unacknowledged</div>
					<div
						class="text-2xl font-semibold {stream.unacknowledged.length ? 'text-warning' : ''}"
					>
						{stream.unacknowledged.length}
					</div>
					<a class="link text-base-content/60 text-xs" href="/ops/notifications">notifications</a>
				</div>
			</div>
		</section>

		<section class="card bg-base-100 shadow-sm">
			<div class="card-body">
				<div class="flex flex-wrap items-center gap-3">
					<h2 class="card-title text-base">Run now</h2>
					<button class="btn btn-ghost btn-xs" onclick={() => (picking = !picking)}>
						{picking ? 'all sources' : 'pick sources'}
					</button>
					{#if form?.error}
						<span class="text-error text-sm">{form.error}</span>
					{:else if form?.run_id}
						<a class="link text-success text-sm" href="/ops/runs/{form.run_id}">
							started {form.jobs} jobs
						</a>
					{/if}
				</div>

				<form method="POST" action="?/run" use:enhance class="mt-2 grid gap-3">
					{#if picking}
						<div
							class="border-base-300 grid max-h-56 grid-cols-2 gap-1 overflow-y-auto rounded border p-2 sm:grid-cols-3 lg:grid-cols-4"
						>
							{#each data.sources ?? [] as source (source.source_id)}
								<label class="flex items-center gap-2 text-sm">
									<input
										type="checkbox"
										class="checkbox checkbox-xs"
										name="sources"
										value={source.source_id}
										bind:group={chosen}
									/>
									<span class="truncate" title={source.label}>{source.source_id}</span>
								</label>
							{/each}
						</div>
					{/if}

					<div class="flex flex-wrap items-center gap-3">
						<label class="flex items-center gap-2 text-sm">
							<span class="text-base-content/60">cache</span>
							<select name="cache_mode" class="select select-bordered select-sm">
								<!-- refresh first and by default: a run under `auto` with a
								     stale max age replays yesterday's pages and reports
								     success while changing no prices. -->
								<option value="refresh">refresh (fetch everything)</option>
								<option value="auto">auto (use what is fresh)</option>
								<option value="replay">replay (offline, no network)</option>
							</select>
						</label>
						<button class="btn btn-primary btn-sm" type="submit">
							Run {picking && chosen.length ? `${chosen.length} sources` : 'all sources'}
						</button>
					</div>
				</form>
			</div>
		</section>

		<section>
			<h2 class="mb-3 text-sm font-semibold uppercase tracking-wide opacity-60">Workers</h2>
			{#if workers.length === 0}
				<p class="text-base-content/60 text-sm">
					No workers have registered. Start one with <code>catalogue-worker</code>.
				</p>
			{:else}
				<div class="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
					{#each workers as worker (worker.worker_id)}
						<WorkerCard {worker} {stream} />
					{/each}
				</div>
			{/if}
		</section>

		<section>
			<div class="mb-3 flex items-center justify-between">
				<h2 class="text-sm font-semibold uppercase tracking-wide opacity-60">Recent runs</h2>
				<a class="link text-sm" href="/ops/runs">all runs</a>
			</div>
			<div class="overflow-x-auto">
				<table class="table-zebra table table-sm bg-base-100 rounded shadow-sm">
					<thead>
						<tr>
							<th>Started</th><th>Kind</th><th>Status</th><th>Sources</th><th>Duration</th>
						</tr>
					</thead>
					<tbody>
						{#each data.runs ?? [] as run (run.id)}
							<tr>
								<td><a class="link" href="/ops/runs/{run.id}">{relative(run.created_at)}</a></td>
								<td>{run.kind}</td>
								<td><span class="badge badge-sm">{run.status}</span></td>
								<td>{run.succeeded}/{run.jobs}{run.failed ? ` · ${run.failed} failed` : ''}</td>
								<td>{duration(run.started_at, run.finished_at)}</td>
							</tr>
						{:else}
							<tr><td colspan="5" class="text-base-content/60">No runs yet.</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>
	</div>
{/if}
