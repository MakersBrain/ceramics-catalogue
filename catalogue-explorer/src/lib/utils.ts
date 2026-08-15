import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Join class names, resolving Tailwind conflicts in favour of the last one.
 *
 * `clsx` flattens the conditional forms - arrays, objects, falsy values - and
 * `twMerge` is what makes a `class` prop on a shared component work at all:
 * without it `cn('px-4', 'px-2')` emits both and the winner is decided by the
 * order Tailwind happened to write them in the stylesheet, not by the caller.
 *
 * That matters more here than in most apps. This is a dense operations
 * dashboard, and the components below are sized for a page of prose: nearly
 * every call site in `ops/` overrides a height or a padding. Those overrides
 * have to win reliably, not by accident of stylesheet order.
 */
export function cn(...inputs: ClassValue[]) {
	return twMerge(clsx(inputs));
}

/**
 * The prop-shape helpers the shadcn-svelte components are generated against.
 *
 * `WithElementRef` is why every component here takes `bind:this` through a
 * `ref` prop rather than exposing the element directly: a caller that needs to
 * measure or focus a control should not have to reach through the component's
 * internals to find it.
 */
export type WithoutChild<T> = T extends { child?: unknown } ? Omit<T, 'child'> : T;
export type WithoutChildren<T> = T extends { children?: unknown } ? Omit<T, 'children'> : T;
export type WithoutChildrenOrChild<T> = WithoutChildren<WithoutChild<T>>;
export type WithElementRef<T, U extends HTMLElement = HTMLElement> = T & { ref?: U | null };
