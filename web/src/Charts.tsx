import {useEffect, useRef} from 'react';
import * as echarts from 'echarts/core';
import {LineChart, BarChart} from 'echarts/charts';
import {GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, MarkAreaComponent, AriaComponent, GraphicComponent} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';
import type {EChartsCoreOption} from 'echarts/core';
import type {LineSeriesOption} from 'echarts/charts';
import type {Slot, Plan, History} from './types';

echarts.use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent,MarkAreaComponent,AriaComponent,GraphicComponent,CanvasRenderer]);
const colors = ['#4285ce','#d87342','#269877','#8b6ad9','#bb6e9e','#b79628','#82909f'];
const time = (value:number, zone:string) => new Intl.DateTimeFormat(undefined,{hour:'2-digit',minute:'2-digit',timeZone:zone}).format(value);
const scale = (value:number|null, divisor=1) => value === null ? null : value/divisor;

export function Chart({option,label,height=300,onSelect}:{option:EChartsCoreOption;label:string;height?:number;onSelect?:(index:number)=>void}) {
  const ref=useRef<HTMLDivElement>(null);
  const instance=useRef<ReturnType<typeof echarts.init>|null>(null);
  const select=useRef(onSelect);
  select.current=onSelect;
  useEffect(()=>{
    const chart=echarts.init(ref.current!);
    instance.current=chart;
    chart.on('click', params=>{ if (typeof params.dataIndex==='number') select.current?.(params.dataIndex); });
    const resize=new ResizeObserver(()=>chart.resize()); resize.observe(ref.current!);
    return ()=>{resize.disconnect();chart.dispose();instance.current=null;};
  },[]);
  useEffect(()=>{instance.current?.setOption(option,{replaceMerge:['series']});},[option]);
  return <div ref={ref} role="img" aria-label={label} style={{height,width:'100%'}}/>;
}

export function ScheduleChart({slots,plan,dark,onSelect}:{slots:Slot[];plan:Plan;dark:boolean;onSelect:(index:number)=>void}) {
  const text=dark?'#c7d5e3':'#4b5e70', grid=dark?'#334455':'#e3eaf0';
  const labels=slots.map(s=>new Date(s.start).getTime());
  const last=slots.at(-1);
  const end=last?new Date(last.start).getTime()+last.duration_hours*3600000:undefined;
  const series:LineSeriesOption[]=[];
  function line(name:string,index:number,values:(number|null)[],color:string,area=false,stack?:string) {
    const data=values.map((v,i)=>[labels[i],v]);
    if(end!==undefined) data.push([end,values.at(-1)??null]);
    series.push({name,type:'line',xAxisIndex:index,yAxisIndex:index,data,step:'end',symbol:'none',connectNulls:false,lineStyle:{width:2,type:name==='House demand'?'dashed':'solid'},itemStyle:{color},areaStyle:area?{opacity:.22}:undefined,stack,
      markLine:{silent:true,symbol:'none',label:{show:index===0,formatter:'Now',color:text},lineStyle:{color:text,type:'dashed'},data:[{xAxis:Date.now()}]}});
  }
  line('Buy · forecast',0,slots.map(s=>s.shadow_import_sek_per_kwh),colors[0]);
  line('Sell · forecast',0,slots.map(s=>s.shadow_export_sek_per_kwh),colors[6]);
  line('Grid import',1,slots.map(s=>scale(s.grid_import_w,1000)),colors[0],true,'supply');
  line('Solar',1,slots.map(s=>scale(s.pv_w,1000)),colors[1],true,'supply');
  line('Battery discharge',1,slots.map(s=>scale(s.battery_discharge_w,1000)),colors[2],true,'supply');
  line('Battery charge',1,slots.map(s=>s.battery_charge_w===null?null:-s.battery_charge_w/1000),colors[2],true,'outgoing');
  line('Grid export',1,slots.map(s=>s.grid_export_w===null?null:-s.grid_export_w/1000),colors[3],true,'outgoing');
  line('House demand',2,slots.map(s=>scale(s.load_w,1000)),colors[4]);
  line('Base load',2,slots.map(s=>scale(s.base_w,1000)),colors[6],true,'consumption');
  const deviceKeys=[...new Set(slots.flatMap(s=>Object.keys(s.device_loads_w||{})))];
  deviceKeys.forEach((key,i)=>line(plan.devices.find(d=>d.key===key)?.name||key,2,slots.map(s=>s.device_loads_w?.[key]==null?null:s.device_loads_w[key]/1000),colors[i%colors.length],true,'consumption'));
  line('House battery',3,slots.map(s=>scale(s.battery_soc,.01)),colors[2]);
  line('Car battery',3,slots.map(s=>scale(s.ev_soc,.01)),colors[3]);
  let cost=0,complete=true;
  line('Projected net cost',4,slots.map(s=>{if(s.import_cost_sek===null||s.export_revenue_sek===null)complete=false; if(!complete)return null; cost+=s.import_cost_sek!-s.export_revenue_sek!;return cost;}),colors[0],true);
  const costSeries=series.at(-1)!;
  const totals=costSeries.data as (number|null)[][];
  costSeries.data=[[labels[0],0],...slots.map((s,i)=>[new Date(s.start).getTime()+s.duration_hours*3600000,totals[i][1]])];
  const tops=[36,204,382,550,718];
  const names=[`Price · ${plan.currency}/kWh`,'Power flows · kW','Consumption · kW','Stored charge · %',`Projected cost · ${plan.currency}`];
  const option:EChartsCoreOption={animation:false,aria:{enabled:true},textStyle:{fontFamily:'system-ui',color:text},
    grid:tops.map((top,i)=>({left:62,right:22,top:top+24,height:i===1?126:116})),
    graphic:names.map((name,i)=>({type:'text',left:14,top:tops[i],style:{text:name,fill:text,fontSize:14,fontWeight:600}})),
    xAxis:tops.map((_,i)=>({type:'time',gridIndex:i,min:labels[0],max:end,splitNumber:5,axisLabel:{show:i===4,color:text,hideOverlap:true,formatter:(value:number)=>time(value,plan.timezone)},axisLine:{show:false},axisTick:{show:false},splitLine:{show:false}})),
    yAxis:tops.map((_,i)=>({type:'value',gridIndex:i,axisLabel:{color:text},splitLine:{lineStyle:{color:grid}}})),
    axisPointer:{link:[{xAxisIndex:'all'}]},tooltip:{trigger:'axis',renderMode:'richText',confine:true},
    dataZoom:[{type:'slider',xAxisIndex:[0,1,2,3,4],bottom:8,height:22,borderColor:grid,textStyle:{color:text}}],series};
  return <Chart option={option} onSelect={i=>onSelect(Math.min(i,slots.length-1))} label="Five aligned forecast charts: price, energy flows, consumption, stored charge and projected cost. Exact values are available in the interval inspector and data table." height={930}/>;
}

export function HistoryChart({history,entryId,dark,resources=false}:{history:History[];entryId?:string;dark:boolean;resources?:boolean}) {
  const lines=resources?[{name:'CPU · %',get:(h:History)=>h.resources?.cpu_percent??null},{name:'Memory · MiB',get:(h:History)=>h.resources? h.resources.memory_usage/1048576:null}]:
    [{name:'Measured house demand · kW',get:(h:History)=>{const v=h.telemetry?.[entryId!]?.measured_w;return v==null?null:v/1000;}},{name:'Plan captured at that time · kW',get:(h:History)=>{const v=h.telemetry?.[entryId!]?.planned_w;return v==null?null:v/1000;}}];
  const text=dark?'#c7d5e3':'#4b5e70';
  return <Chart label={resources?'Container CPU and memory history. Latest values are shown above.':'Measured house demand compared with the forecast captured at each sample. Values are also available in the table below.'} option={{animation:false,color:colors,legend:{textStyle:{color:text},bottom:0},grid:{left:58,right:58,top:22,bottom:66},tooltip:{trigger:'axis',renderMode:'richText',confine:true},xAxis:{type:'time',splitNumber:4,axisLabel:{color:text,hideOverlap:true}},yAxis:[{type:'value',axisLabel:{color:text},name:resources?'%':'kW'},...(resources?[{type:'value',axisLabel:{color:text},name:'MiB'}]:[])],series:lines.map((line,i)=>({type:'line',name:line.name,showSymbol:false,connectNulls:false,step:i===1?'end':undefined,yAxisIndex:resources?i:0,data:history.map(h=>[new Date(h.sampled_at).getTime(),line.get(h)])}))}}/>;
}
