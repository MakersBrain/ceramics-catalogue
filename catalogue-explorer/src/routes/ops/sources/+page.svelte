<script lang="ts">
	import { enhance } from '$app/forms';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { count, delta, relative, staleness } from '$lib/ops/format';
	import * as Table from '$lib/components/ui/table';
	import { StatusBadge } from '$lib/components/ui/status-badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { NativeSelect } from '$lib/components/ui/native-select';
	import { Checkbox } from '$lib/components/ui/checkbox';
	import { Notice } from '$lib/components/ui/notice';

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
		<Input class="h-8 w-48 text-xs" placeholder="filter" bind:value={filter} />
		<label class="flex items-center gap-2 text-sm">
			<Checkbox bind:checked={onlyProblems} />
			needs attention
		</label>
		<span class="text-muted-foreground ml-auto text-sm">{rows.length} shown</span>
	</div>

	{#if form?.error}
		<Notice kind="error" class="mb-4">{form.error}</Notice>
	{/if}

	<p class="text-muted-foreground mb-3 text-sm">
		The staleness badge is the point of this page: a source that silently stopped returning
		records is the failure the whole pipeline exists to catch, and nothing else surfaces it.
	</p>

	<div class="bg-card overflow-hidden rounded-lg border">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Source</Table.Head>
					<Table.Head>Scraper</Table.Head>
					<Table.Head>Last success</Table.Head>
					<Table.Head class="text-right">Records</Table.Head>
					<Table.Head class="text-right">Δ</Table.Head>
					<Table.Head class="text-right">7d fails</Table.Head>
					<Table.Head>Controls</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each rows as source (source.source_id)}
					{@const stale = staleness(source.staleness_seconds)}
					{@const change = delta(source.last_records, source.previous_records)}
					<Table.Row>
						<Table.Cell>
							{#if source.last_job_id && source.last_run_id}
								<a
									class="text-accent-foreground font-medium underline-offset-4 hover:underline"
									href="/ops/runs/{source.last_run_id}/jobs/{source.last_job_id}"
									>{source.source_id}</a
								>
							{:else}
								<div class="font-medium">{source.source_id}</div>
							{/if}
							<div class="text-muted-foreground/70 max-w-56 truncate text-xs">{source.label}</div>
						</Table.Cell>
						<Table.Cell class="text-xs">{source.scraper}</Table.Cell>
						<Table.Cell>
							<StatusBadge tone={stale.tone} title={source.last_success_at ?? 'never'}>
								{source.last_success_at ? `${stale.label} ago` : 'never'}
							</StatusBadge>
						</Table.Cell>
						<Table.Cell class="text-right tabular-nums">{count(source.last_records)}</Table.Cell>
						<Table.Cell class="text-right tabular-nums">
							{#if change}
								<span class={change.share < -30 ? 'text-destructive font-medium' : change.share < 0 ? 'text-warning' : 'opacity-60'}>
									{change.change > 0 ? '+' : ''}{count(change.change)}
									<span class="text-xs">({change.share.toFixed(0)}%)</span>
								</span>
							{:else}
								<span class="text-muted-foreground/60">—</span>
							{/if}
						</Table.Cell>
						<Table.Cell
							class="text-right tabular-nums {source.failures_7d
								? 'text-destructive'
								: 'text-muted-foreground/60'}"
						>
							{source.failures_7d}
						</Table.Cell>
						<Table.Cell>
							<form method="POST" action="?/update" use:enhance class="flex items-center gap-2">
								<input type="hidden" name="id" value={source.source_id} />
								<!-- Both of these were a daisyUI `toggle` and a `checkbox`
								     respectively, which read as two different kinds of control
								     for what are two booleans on the same form. They are the
								     same control now. -->
								<label
									class="flex items-center gap-1 text-xs"
									title="excluded from future runs when off"
								>
									<Checkbox name="enabled" checked={source.enabled} />
									enabled
								</label>
								<label
									class="flex items-center gap-1 text-xs"
									title="queued jobs are not claimed while paused"
								>
									<Checkbox name="paused" checked={source.paused} />
									paused
								</label>
								<NativeSelect name="schedule_id" class="h-7 text-xs" wrapperClass="w-28">
									<option value="">default</option>
									{#each data.schedules ?? [] as schedule (schedule.id)}
										<option value={schedule.id} selected={source.schedule_id === schedule.id}>
											{schedule.id}
										</option>
									{/each}
								</NativeSelect>
								<Button variant="secondary" size="xs" type="submit">save</Button>
							</form>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</div>
{/if}
