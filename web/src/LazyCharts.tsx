import {lazy,Suspense} from 'react';
import type {ComponentProps} from 'react';
import type * as Charts from './Charts';
const History=lazy(()=>import('./Charts').then(module=>({default:module.HistoryChart})));
export function HistoryChart(props:ComponentProps<typeof Charts.HistoryChart>){return <Suspense fallback={<p role="status">Loading observations…</p>}><History {...props}/></Suspense>;}
