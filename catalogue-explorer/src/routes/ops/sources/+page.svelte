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
	import { Switch } from '$lib/components/ui/switch';
	import { Notice } from '$lib/components/ui/notice';
	import Check from '@lucide/svelte/icons/check';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import Power from '@lucide/svelte/icons/power';
	import Square from '@lucide/svelte/icons/square';
	import X from '@lucide/svelte/icons/x';

	let { data, form } = $props();

	let filter = $state('');
	let onlyProblems = $state(false);
	let selected = $state<string[]>([]);

	const rows = $derived(
		(data.sources ?? [])
			.filter((s: any) => !filter || s.source_id.includes(filter) || s.label?.toLowerCase().includes(filter.toLowerCase()))
			.filter((s: any) => !onlyProblems || problem(s))
	);
	const allShownSelected = $derived(
		rows.length > 0 && rows.every((source: any) => selected.includes(source.source_id))
	);
	const someShownSelected = $derived(
		rows.some((source: any) => selected.includes(source.source_id)) && !allShownSelected
	);

	function select(id: string, checked: boolean) {
		selected = checked
			? [...new Set([...selected, id])]
			: selected.filter((selectedId) => selectedId !== id);
	}

	function selectShown(checked: boolean) {
		const shown = new Set(rows.map((source: any) => source.source_id));
		selected = checked
			? [...new Set([...selected, ...shown])]
			: selected.filter((id) => !shown.has(id));
	}

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
	{:else if form?.updated}
		<Notice kind="success" class="mb-4">Updated {form.updated} sources.</Notice>
	{/if}

	<p class="text-muted-foreground mb-3 text-sm">
		The staleness badge is the point of this page: a source that silently stopped returning
		records is the failure the whole pipeline exists to catch, and nothing else surfaces it.
	</p>

	{#if selected.length}
		<div class="border-border bg-muted/50 mb-3 flex flex-wrap items-center gap-2 rounded-lg border px-2.5 py-2">
			<span class="text-sm font-medium tabular-nums">{selected.length} selected</span>
			<form method="POST" action="?/bulk" use:enhance class="flex flex-wrap items-center gap-1.5">
				{#each selected as id (id)}
					<input type="hidden" name="ids" value={id} />
				{/each}
				<Button variant="secondary" size="xs" type="submit" name="action" value="enable" title="Turn selected sources on">
					<Power aria-hidden="true" /> On
				</Button>
				<Button variant="secondary" size="xs" type="submit" name="action" value="disable" title="Turn selected sources off">
					<Square aria-hidden="true" /> Off
				</Button>
				<Button variant="secondary" size="xs" type="submit" name="action" value="pause" title="Pause selected sources">
					<Pause aria-hidden="true" /> Pause
				</Button>
				<Button variant="secondary" size="xs" type="submit" name="action" value="resume" title="Resume selected sources">
					<Play aria-hidden="true" /> Resume
				</Button>
				<div class="bg-border mx-0.5 h-5 w-px" aria-hidden="true"></div>
				<NativeSelect name="schedule_id" class="h-7 text-xs" wrapperClass="w-28" aria-label="Schedule for selected sources">
					<option value="">default</option>
					{#each data.schedules ?? [] as schedule (schedule.id)}
						<option value={schedule.id}>{schedule.id}</option>
					{/each}
				</NativeSelect>
				<Button variant="secondary" size="xs" type="submit" name="action" value="schedule">Set schedule</Button>
			</form>
			<Button variant="ghost" size="icon-xs" class="ml-auto" type="button" onclick={() => (selected = [])} title="Clear selection" aria-label="Clear source selection">
				<X aria-hidden="true" />
			</Button>
		</div>
	{/if}

	<div class="bg-card overflow-hidden rounded-lg border">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head class="w-9 pr-0">
						<Checkbox
							checked={allShownSelected}
							indeterminate={someShownSelected}
							onchange={(event: Event) => selectShown((event.currentTarget as HTMLInputElement).checked)}
							aria-label="Select all shown sources"
						/>
					</Table.Head>
					<Table.Head>Source</Table.Head>
					<Table.Head>Scraper</Table.Head>
					<Table.Head>Last success</Table.Head>
					<Table.Head class="text-right">Records</Table.Head>
					<Table.Head class="text-right">Δ</Table.Head>
					<Table.Head class="text-right">7d fails</Table.Head>
					<Table.Head class="w-px">Controls</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each rows as source (source.source_id)}
					{@const stale = staleness(source.staleness_seconds)}
					{@const change = delta(source.last_records, source.previous_records)}
					<Table.Row data-state={selected.includes(source.source_id) ? 'selected' : undefined}>
						<Table.Cell class="w-9 pr-0">
							<Checkbox
								checked={selected.includes(source.source_id)}
								onchange={(event: Event) => select(source.source_id, (event.currentTarget as HTMLInputElement).checked)}
								aria-label={`Select ${source.source_id}`}
							/>
						</Table.Cell>
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
						<Table.Cell class="whitespace-nowrap">
							<form method="POST" action="?/update" use:enhance class="flex items-center gap-1.5">
								<input type="hidden" name="id" value={source.source_id} />
								<label
									class="flex items-center"
									title="excluded from future runs when off"
								>
									<Switch
										name="enabled"
										bind:checked={source.enabled}
										aria-label={`${source.source_id} enabled`}
									/>
									<span class="sr-only">{source.enabled ? 'on' : 'off'}</span>
								</label>
								<label
									class="cursor-pointer"
									title="queued jobs are not claimed while paused"
								>
									<Checkbox class="peer sr-only" name="paused" bind:checked={source.paused} />
									<span class="border-input text-muted-foreground hover:bg-secondary peer-checked:border-warning/40 peer-checked:bg-warning/10 peer-checked:text-warning flex size-7 items-center justify-center rounded-md border transition-colors">
										<Pause class="size-3" aria-hidden="true" />
										<span class="sr-only">paused</span>
									</span>
								</label>
								<NativeSelect name="schedule_id" class="h-7 px-2 pr-6 text-xs" wrapperClass="w-24" aria-label={`Schedule for ${source.source_id}`}>
									<option value="">default</option>
									{#each data.schedules ?? [] as schedule (schedule.id)}
										<option value={schedule.id} selected={source.schedule_id === schedule.id}>
											{schedule.id}
										</option>
									{/each}
								</NativeSelect>
								<Button variant="secondary" size="icon-xs" type="submit" title={`Save ${source.source_id}`} aria-label={`Save ${source.source_id}`}>
									<Check aria-hidden="true" />
								</Button>
							</form>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</div>
{/if}
