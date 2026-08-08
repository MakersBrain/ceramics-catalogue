<script lang="ts">
	import { enhance } from '$app/forms';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, severityTone } from '$lib/ops/format';

	let { data, form } = $props();

	// Unacknowledged first: this is a work list, and everything already dealt
	// with is history.
	const open = $derived(
		(data.notifications ?? []).filter((n: any) => !n.acknowledged_at && !n.resolved_at)
	);
	const closed = $derived(
		(data.notifications ?? []).filter((n: any) => n.acknowledged_at || n.resolved_at)
	);

	function link(entry: any): string | null {
		if (entry.run_id) return `/ops/runs/${entry.run_id}`;
		if (entry.source_id) return `/ops/sources`;
		return null;
	}
</script>

<svelte:head><title>Notifications · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<div class="mb-4 flex flex-wrap items-center gap-3">
		<h1 class="text-lg font-semibold">Notifications</h1>
		<form method="GET" class="flex items-center gap-2">
			<select name="severity" class="select select-bordered select-sm" value={data.severity ?? ''}>
				<option value="">all severities</option>
				<option value="critical">critical</option>
				<option value="warning">warning</option>
				<option value="info">info</option>
			</select>
			<button class="btn btn-sm" type="submit">Filter</button>
		</form>
	</div>

	{#if form?.error}
		<div class="alert alert-error mb-4 text-sm">{form.error}</div>
	{/if}

	<section class="mb-8">
		<h2 class="mb-2 text-sm font-semibold uppercase opacity-60">
			Needs attention ({open.length})
		</h2>
		{#if open.length === 0}
			<p class="text-base-content/60 text-sm">Nothing outstanding.</p>
		{:else}
			<ul class="grid gap-2">
				{#each open as entry (entry.id)}
					<li class="card bg-base-100 shadow-sm">
						<div class="card-body flex-row items-start gap-3 p-4">
							<span class="badge {severityTone(entry.severity)} badge-sm">{entry.severity}</span>
							<div class="min-w-0 flex-1">
								<div class="font-medium">{entry.title}</div>
								{#if entry.body}
									<p class="text-base-content/60 mt-0.5 text-sm">{entry.body}</p>
								{/if}
								<div class="text-base-content/40 mt-1 text-xs">
									{entry.kind} · {relative(entry.at)}
									{#if link(entry)}
										· <a class="link" href={link(entry)}>{entry.source_id ?? 'run'}</a>
									{/if}
								</div>
							</div>
							<form method="POST" action="?/ack" use:enhance>
								<input type="hidden" name="id" value={entry.id} />
								<button class="btn btn-sm" type="submit">Acknowledge</button>
							</form>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section>
		<h2 class="mb-2 text-sm font-semibold uppercase opacity-60">Resolved and acknowledged</h2>
		<div class="overflow-x-auto">
			<table class="table-zebra table table-sm bg-base-100 rounded shadow-sm">
				<thead>
					<tr><th>When</th><th>Severity</th><th>Kind</th><th>Title</th><th>Closed</th></tr>
				</thead>
				<tbody>
					{#each closed as entry (entry.id)}
						<tr>
							<td>{relative(entry.at)}</td>
							<td><span class="badge badge-sm {severityTone(entry.severity)}">{entry.severity}</span></td>
							<td class="text-xs">{entry.kind}</td>
							<td class="max-w-96 truncate">{entry.title}</td>
							<td class="text-xs opacity-60">
								{entry.resolved_at ? 'resolved' : `acknowledged by ${entry.acknowledged_by ?? '—'}`}
							</td>
						</tr>
					{:else}
						<tr><td colspan="5" class="text-base-content/60">Nothing yet.</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}
