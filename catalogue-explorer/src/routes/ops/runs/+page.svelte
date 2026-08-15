<script lang="ts">
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';
	import * as Table from '$lib/components/ui/table';
	import { StatusBadge } from '$lib/components/ui/status-badge';
	import { Button } from '$lib/components/ui/button';

	let { data } = $props();
</script>

<svelte:head><title>Runs · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<h1 class="mb-4 text-lg font-semibold">Run history</h1>

	<!--
		No zebra striping, where this table used to have it. The rows carry a
		status of their own and a failure count that turns red; a stripe underneath
		that is a second, weaker answer to the question the tint already answers.
	-->
	<div class="bg-card overflow-hidden rounded-lg border">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Started</Table.Head>
					<Table.Head>Kind</Table.Head>
					<Table.Head>Requested by</Table.Head>
					<Table.Head>Status</Table.Head>
					<Table.Head class="text-right">ok</Table.Head>
					<Table.Head class="text-right">failed</Table.Head>
					<Table.Head class="text-right">records</Table.Head>
					<Table.Head>Duration</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each data.runs ?? [] as run (run.id)}
					<Table.Row>
						<Table.Cell>
							<a
								class="text-accent-foreground underline-offset-4 hover:underline"
								href="/ops/runs/{run.id}"
								title={run.created_at}
							>
								{relative(run.created_at)}
							</a>
						</Table.Cell>
						<Table.Cell>{run.kind}</Table.Cell>
						<Table.Cell class="text-muted-foreground max-w-40 truncate">
							{run.requested_by ?? '—'}
						</Table.Cell>
						<Table.Cell><StatusBadge tone={stateTone(run.status)}>{run.status}</StatusBadge></Table.Cell>
						<Table.Cell class="text-right tabular-nums">{run.succeeded}</Table.Cell>
						<Table.Cell class="text-right tabular-nums {run.failed ? 'text-destructive' : ''}">
							{run.failed}
						</Table.Cell>
						<Table.Cell class="text-right tabular-nums">{count(run.summary?.records)}</Table.Cell>
						<Table.Cell>{duration(run.started_at, run.finished_at)}</Table.Cell>
					</Table.Row>
				{:else}
					<Table.Row>
						<Table.Cell colspan={8} class="text-muted-foreground">
							No runs recorded yet. Start one from the overview, or run
							<code>catalogue-dump --record</code>.
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</div>

	{#if data.next_cursor}
		<Button
			variant="secondary"
			size="sm"
			class="mt-4"
			href="/ops/runs?cursor={encodeURIComponent(data.next_cursor)}"
		>
			Older runs
		</Button>
	{/if}
{/if}
