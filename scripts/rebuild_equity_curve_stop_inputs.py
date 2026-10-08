"""Private historical recollection: writes isolated raw inputs only; never calls broker."""
from pathlib import Path
import argparse, concurrent.futures, datetime as dt, hashlib, json, os, sys, threading, time, urllib.request
import pandas as pd
import tushare as ts


ENDPOINTS={
 'daily': ('daily', ['trade_date','ts_code','open','high','low','close','pre_close','vol','amount','pct_chg'],6000),
 'daily_basic': ('daily_basic',['trade_date','ts_code','turnover_rate','turnover_rate_f','volume_ratio','float_share','free_share','total_mv','circ_mv'],6000),
 'adj_factor': ('adj_factor',['trade_date','ts_code','adj_factor'],6000),
 'limit_list': ('limit_list_d',['trade_date','ts_code','name','close','pct_chg','fd_amount','first_time','last_time','open_times','limit_times','limit'],2500),
}

def validate(frame,date,kind,max_null_ratio=0.05):
 required=set(ENDPOINTS[kind][1]); missing=required-set(frame.columns)
 if missing: raise ValueError('missing columns '+','.join(sorted(missing)))
 if frame.empty: raise ValueError('API returned no rows')
 if not frame['trade_date'].astype(str).eq(date).all(): raise ValueError('wrong trade_date')
 if frame[['trade_date','ts_code']].duplicated().any(): raise ValueError('duplicate trade_date/ts_code')
 if frame['ts_code'].isna().any(): raise ValueError('missing stock code')
 positive={'daily':['open','high','low','close','pre_close'],'adj_factor':['adj_factor'],'daily_basic':[],'limit_list':['close']}
 for col in positive[kind]:
  values=pd.to_numeric(frame[col],errors='coerce')
  if values.isna().any() or values.le(0).any(): raise ValueError('invalid '+col)
 if kind=='daily_basic':
  ratios={c:float(pd.to_numeric(frame[c],errors='coerce').isna().mean()) for c in ['volume_ratio','turnover_rate_f','free_share']}
  if any(v>float(max_null_ratio) for v in ratios.values()): raise ValueError('incomplete basic '+json.dumps(ratios))
 if kind=='limit_list':
  if not frame['limit'].eq('U').all(): raise ValueError('wrong limit type')
  for col in ['fd_amount','open_times','limit_times']:
   values=pd.to_numeric(frame[col],errors='coerce')
   if values.isna().any() or values.lt(0).any(): raise ValueError('missing or negative '+col)
 return len(frame)

def main():
 parser=argparse.ArgumentParser(description='重新采集方案甲历史输入到隔离目录；不调用券商、不生成开仓判定。')
 parser.add_argument('--start-date',default='20190101');parser.add_argument('--end-date',required=True)
 parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
 parser.add_argument('--requests-per-minute',type=int,default=120)
 parser.add_argument('--progress-url')
 parser.add_argument('--output',required=True);parser.add_argument('--probe-only',action='store_true')
 args=parser.parse_args(); ROOT=args.project_root.absolute();target=Path(args.output).absolute()
 if not 1<=args.requests_per_minute<=200:raise ValueError('requests-per-minute must be 1..200 per endpoint')
 sys.path.insert(0,str(ROOT))
 from src.secret_config import load_tushare_token
 if target==ROOT or ROOT in target.parents: raise RuntimeError('output must be outside production')
 target.mkdir(parents=True,exist_ok=True)
 config=json.loads((ROOT/'config/config.json').read_text(encoding='utf-8'))
 token=load_tushare_token(config,project_root=ROOT)
 if not token:raise RuntimeError('TUSHARE_TOKEN missing')
 calendar=pd.read_csv(ROOT/'data/raw/trade_calendar.csv',dtype={'cal_date':str})
 dates=sorted(calendar.loc[pd.to_numeric(calendar.is_open).eq(1),'cal_date'].astype(str))
 dates=[d for d in dates if args.start_date<=d<=args.end_date]
 if not dates or dates[-1]!=args.end_date:raise RuntimeError('calendar does not cover requested end')
 lock=threading.Lock()
 state={'status':'RUNNING','broker_calls_sent':False,'production_writes':False,'requested_start':args.start_date,'requested_end':args.end_date,'output':str(target),'expected_trade_days':len(dates),'started_at':dt.datetime.now().astimezone().isoformat(),'kinds':{},'probes':{}}
 def save():
  with lock:
   state['updated_at']=dt.datetime.now().astimezone().isoformat();body=json.dumps(state,ensure_ascii=False,indent=2)
   tmp=target/'status.json.tmp';tmp.write_text(body,encoding='utf-8');tmp.replace(target/'status.json')
  try:
   if not args.progress_url:return
   req=urllib.request.Request(args.progress_url,data=body.encode(),headers={'Content-Type':'application/json'})
   urllib.request.urlopen(req,timeout=5).read()
  except Exception:pass
 def query(pro,api,date,fields,cap,**kwargs):
  chunks=[];offset=0
  while True:
   time.sleep(60.0/args.requests_per_minute)
   part=pro.query(api,trade_date=date,fields=fields,limit=cap,offset=offset,**kwargs)
   chunks.append(part)
   if len(part)<cap:break
   offset+=len(part)
  return pd.concat(chunks,ignore_index=True)
 pro=ts.pro_api(token,timeout=30)
 for api,date,kwargs in [('limit_list_d','20190102',{'limit_type':'U'}),('limit_list_d','20191128',{'limit_type':'U'}),('limit_list','20190102',{'limit_type':'U'}),('kpl_list','20190102',{'tag':'涨停'})]:
  try:
   frame=query(pro,api,date,'',2500,**kwargs)
   entry={'rows':len(frame),'columns':list(frame.columns),'dates': sorted(frame.trade_date.astype(str).unique().tolist()) if 'trade_date' in frame else []}
   if len(frame):
    probe_path=target/'probes'/f'{api}_{date}.csv';probe_path.parent.mkdir(exist_ok=True);frame.to_csv(probe_path,index=False,encoding='utf-8-sig')
  except Exception as exc:entry={'error':str(exc).replace(token,'[REDACTED]')[:600]}
  state['probes'][api+'_'+date]=entry;save()
 if args.probe_only:
  state['status']='PROBED';save();print('HISTORY_PROBES_SAVED');return
 def worker(kind):
  api,required,cap=ENDPOINTS[kind];pro=ts.pro_api(token,timeout=30)
  folder=target/'data/raw'/kind;folder.mkdir(parents=True,exist_ok=True)
  fields=config['collection'].get('limit_list_fields' if kind=='limit_list' else kind+'_fields','')
  fields=','.join(dict.fromkeys([*str(fields or '').split(','),*required]));fields=fields.strip(',')
  record={'saved':0,'reused':0,'checked':0,'failed':{},'unsupported_dates':[], 'api':api,'last_date':None,'last_rows':None}
  manifest={}
  def publish():
   snapshot=json.loads(json.dumps({k:v for k,v in record.items() if k!='manifest'}))
   with lock:state['kinds'][kind]=snapshot
  publish()
  for i,date in enumerate(dates):
   if kind=='limit_list' and date<str(config.get('cleaning',{}).get('limit_list_start_date','20191128')):
    record['unsupported_dates'].append(date);record['checked']+=1;continue
   path=folder/(date+'.csv');record['last_date']=date
   succeeded=False
   for attempt in range(3):
    try:
     reused=path.exists()
     max_null_ratio=float(config.get('collection',{}).get('daily_basic_max_null_ratio',0.05))
     if reused:
      try:
       frame=pd.read_csv(path,dtype={'trade_date':str,'ts_code':str})
       validate(frame,date,kind,max_null_ratio)
      except (ValueError,UnicodeError,pd.errors.ParserError,pd.errors.EmptyDataError):
       rejected=target/'rejected_cache'/kind;rejected.mkdir(parents=True,exist_ok=True)
       path.replace(rejected/(date+'_'+str(time.time_ns())+'.csv'))
       reused=False
     if not reused:frame=query(pro,api,date,fields,cap,**({'limit_type':'U'} if kind=='limit_list' else {}))
     rows=validate(frame,date,kind,max_null_ratio)
     if not reused:
      if kind=='limit_list':
       frame['limit_data_source']='limit_list_d';frame['limit_data_quality']='full';frame['strategy_compatible']=True
      tmp=path.with_suffix('.csv.tmp');frame.to_csv(tmp,index=False,encoding='utf-8-sig');tmp.replace(path)
     digest=hashlib.sha256(path.read_bytes()).hexdigest()
     manifest[date]={'rows':rows,'sha256':digest,'bytes':path.stat().st_size}
     record['reused' if reused else 'saved']+=1;record['last_rows']=rows;succeeded=True;break
    except Exception as exc:
     message=str(exc).replace(token,'[REDACTED]')[:400]
     if attempt<2:time.sleep(3*(attempt+1))
   if not succeeded:record['failed'][date]=message
   record['checked']+=1
   if i%20==0 or not succeeded:publish();save();print(kind,record['checked'],'/',len(dates),date,'OK' if succeeded else message,flush=True)
  (folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
  record['finished']=True;publish();save()
 save()
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  futures=[pool.submit(worker,k) for k in ENDPOINTS]
  for f in futures:f.result()
 state['status']='INPUTS_COLLECTED_NEEDS_VALIDATION' if all(not r['failed'] for r in state['kinds'].values()) else 'INCOMPLETE'
 state['unsupported_history_note']='Pre-20191128 limit_list_d has no approved replay input; probes retained for investigation; no stop decision is generated.'
 save();print('ISOLATED_HISTORICAL_RECOLLECTION_FINISHED',state['status'],flush=True)
if __name__=='__main__':main()
