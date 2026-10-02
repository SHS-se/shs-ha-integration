import {useEffect, useRef} from 'react';
import * as echarts from 'echarts/core';
import {LineChart} from 'echarts/charts';
import {GridComponent, TooltipComponent, LegendComponent} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';
import type {EChartsCoreOption} from 'echarts/core';
import type {History} from './types';

echarts.use([LineChart,GridComponent,TooltipComponent,LegendComponent,CanvasRenderer]);
const colors = ['#4285ce','#d87342','#269877','#8b6ad9','#bb6e9e','#b79628','#82909f'];

export function Chart({option,label,height=300}:{option:EChartsCoreOption;label:string;height?:number}) {
  const ref=useRef<HTMLDivElement>(null);
  const instance=useRef<ReturnType<typeof echarts.init>|null>(null);
  useEffect(()=>{
    const chart=echarts.init(ref.current!);
    instance.current=chart;
    const resize=new ResizeObserver(()=>chart.resize()); resize.observe(ref.current!);
    return ()=>{resize.disconnect();chart.dispose();instance.current=null;};
  },[]);
  useEffect(()=>{instance.current?.setOption(option,{replaceMerge:['series']});},[option]);
  return <div ref={ref} role="img" aria-label={label} style={{height,width:'100%'}}/>;
}

export function HistoryChart({history,entryId,dark,resources=false}:{history:History[];entryId?:string;dark:boolean;resources?:boolean}) {
  const lines=resources?[{name:'CPU · %',get:(h:History)=>h.resources?.cpu_percent??null},{name:'Memory · MiB',get:(h:History)=>h.resources? h.resources.memory_usage/1048576:null}]:
    [{name:'Measured house demand · kW',get:(h:History)=>{const v=h.telemetry?.[entryId!]?.measured_w;return v==null?null:v/1000;}},{name:'Plan captured at that time · kW',get:(h:History)=>{const v=h.telemetry?.[entryId!]?.planned_w;return v==null?null:v/1000;}}];
  const text=dark?'#c7d5e3':'#4b5e70';
  return <Chart label={resources?'Container CPU and memory history. Latest values are shown above.':'Measured house demand compared with the forecast captured at each sample. Values are also available in the table below.'} option={{animation:false,color:colors,legend:{textStyle:{color:text},bottom:0},grid:{left:58,right:58,top:22,bottom:66},tooltip:{trigger:'axis',renderMode:'richText',confine:true},xAxis:{type:'time',splitNumber:4,axisLabel:{color:text,hideOverlap:true}},yAxis:[{type:'value',axisLabel:{color:text},name:resources?'%':'kW'},...(resources?[{type:'value',axisLabel:{color:text},name:'MiB'}]:[])],series:lines.map((line,i)=>({type:'line',name:line.name,showSymbol:false,connectNulls:false,step:i===1?'end':undefined,yAxisIndex:resources?i:0,data:history.map(h=>[new Date(h.sampled_at).getTime(),line.get(h)])}))}}/>;
}
