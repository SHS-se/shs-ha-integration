import {lazy,Suspense} from 'react';
import type {ComponentProps} from 'react';
import type * as Charts from './Charts';
const Schedule=lazy(()=>import('./Charts').then(module=>({default:module.ScheduleChart})));
const History=lazy(()=>import('./Charts').then(module=>({default:module.HistoryChart})));
export function ScheduleChart(props:ComponentProps<typeof Charts.ScheduleChart>){return <Suspense fallback={<p role="status">Loading schedule charts…</p>}><Schedule {...props}/></Suspense>;}
export function HistoryChart(props:ComponentProps<typeof Charts.HistoryChart>){return <Suspense fallback={<p role="status">Loading observations…</p>}><History {...props}/></Suspense>;}
