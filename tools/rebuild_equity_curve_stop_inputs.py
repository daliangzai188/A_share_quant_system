"""Private historical recollection: writes isolated raw inputs only; never calls broker."""
from pathlib import Path
import argparse, concurrent.futures, datetime as dt, hashlib, json, os, sys, threading, time, urllib.request
import pandas as pd
import tushare as ts

MODULE_ROOT=Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:sys.path.insert(0,str(MODULE_ROOT))
from src.limit_source_quality import suspended_placeholder_mask


ENDPOINTS={
 'daily': ('daily', ['trade_date','ts_code','open','high','low','close','pre_close','vol','amount','pct_chg'],6000),
 'daily_basic': ('daily_basic',['trade_date','ts_code','turnover_rate','turnover_rate_f','volume_ratio','float_share','free_share','total_mv','circ_mv'],6000),
 'adj_factor': ('adj_factor',['trade_date','ts_code','adj_factor'],6000),
 'limit_list': ('limit_list_d',['trade_date','ts_code','name','close','pct_chg','fd_amount','first_time','last_time','open_times','limit_times','limit'],2500),
}

def mapped_pre_listing_quotes(frame, listing_dates=None):
 # 北交所上市前的新三板历史被供应商映射为现行股票代码，可能没有前次报价。
 # 有个股上市日期时按该日期判断；未知股票只能使用北交所正式开市日期。
 boundaries=frame['ts_code'].map(listing_dates or {}).fillna('20211115').astype(str)
 return (frame['ts_code'].astype(str).str.endswith('.BJ') &
         frame['trade_date'].astype(str).lt(boundaries) &
         pd.to_numeric(frame['pre_close'],errors='coerce').isna())

def validate(frame,date,kind,max_null_ratio=0.05,exclude_bj=True,listing_dates=None,daily_codes=None):
 required=set(ENDPOINTS[kind][1]); missing=required-set(frame.columns)
 if missing: raise ValueError('missing columns '+','.join(sorted(missing)))
 if frame.empty: raise ValueError('API returned no rows')
 if not frame['trade_date'].astype(str).eq(date).all(): raise ValueError('wrong trade_date')
 if frame[['trade_date','ts_code']].duplicated().any(): raise ValueError('duplicate trade_date/ts_code')
 if frame['ts_code'].isna().any(): raise ValueError('missing stock code')
 # 保留供应商原始行；数值质量闸门使用与研究清洗器相同的市场范围。
 # 北交所代码映射到早期新三板历史时，pre_close可能为空，不能据此否决沪深整日数据。
 scoped=frame.loc[~frame['ts_code'].astype(str).str.endswith('.BJ')] if exclude_bj else frame
 if kind=='limit_list' and daily_codes is not None:
  scoped=scoped.loc[~suspended_placeholder_mask(scoped,daily_codes)]
 if scoped.empty: raise ValueError('API returned no rows in configured market scope')
 positive={'daily':['open','high','low','close','pre_close'],'adj_factor':['adj_factor'],'daily_basic':[],'limit_list':['close']}
 for col in positive[kind]:
  values=pd.to_numeric(scoped[col],errors='coerce')
  # 供应商把开市前的新三板行情映射为现行.BJ代码，昨收可能确实没有记录。
  # 只原样保留这一明确的历史缺口，不补价格；沪深及北交所开市后的缺口仍失败。
  known_legacy_missing=mapped_pre_listing_quotes(scoped,listing_dates) if kind=='daily' and col=='pre_close' else pd.Series(False,index=scoped.index)
  if (values.isna() & ~known_legacy_missing).any() or values.le(0).any(): raise ValueError('invalid '+col)
 if kind=='daily_basic':
  ratios={c:float(pd.to_numeric(scoped[c],errors='coerce').isna().mean()) for c in ['volume_ratio','turnover_rate_f','free_share']}
  if any(v>float(max_null_ratio) for v in ratios.values()): raise ValueError('incomplete basic '+json.dumps(ratios))
 if kind=='limit_list':
  if not frame['limit'].eq('U').all(): raise ValueError('wrong limit type')
  for col in ['fd_amount','open_times','limit_times']:
   values=pd.to_numeric(scoped[col],errors='coerce')
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
 reference=ROOT/'data/raw/stock_basic/stock_basic_all.csv'
 listing_dates={}
 if reference.exists():
  stock=pd.read_csv(reference,dtype=str)
  if {'ts_code','list_date'}.issubset(stock.columns):
   listing_dates={str(row.ts_code):str(row.list_date) for row in stock.itertuples() if isinstance(row.list_date,str) and len(row.list_date)==8 and row.list_date.isdigit()}
 token=load_tushare_token(config,project_root=ROOT)
 if not token:raise RuntimeError('TUSHARE_TOKEN missing')
 calendar=pd.read_csv(ROOT/'data/raw/trade_calendar.csv',dtype={'cal_date':str})
 dates=sorted(calendar.loc[pd.to_numeric(calendar.is_open).eq(1),'cal_date'].astype(str))
 dates=[d for d in dates if args.start_date<=d<=args.end_date]
 if not dates or dates[-1]!=args.end_date:raise RuntimeError('calendar does not cover requested end')
 lock=threading.Lock()
 exclude_bj=bool(config.get('cleaning',{}).get('exclude_bj',True))
 state={'status':'RUNNING','broker_calls_sent':False,'production_writes':False,'requested_start':args.start_date,'requested_end':args.end_date,'output':str(target),'expected_trade_days':len(dates),'started_at':dt.datetime.now().astimezone().isoformat(),'kinds':{},'probes':{},'validation_market_scope':'SH/SZ; BJ raw rows preserved without imputation' if exclude_bj else 'all raw markets','known_source_gap_policy':'BJ mapped pre-listing NEEQ quotes may have unknown pre_close. Validate against stock_basic listing dates; preserve raw missing values and record affected days. No price imputation.','stock_reference_sha256':hashlib.sha256(reference.read_bytes()).hexdigest() if reference.exists() else None}
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
  record={'saved':0,'reused':0,'checked':0,'failed':{},'unsupported_dates':[], 'api':api,'last_date':None,'last_rows':None,'known_source_gap_days':{}}
  manifest={}
  def daily_quote_codes(frame,date):
   if kind!='limit_list' or not suspended_placeholder_mask(frame,set()).any():return None
   daily_path=target/'data/raw/daily'/(date+'.csv')
   if daily_path.exists():return set(pd.read_csv(daily_path,usecols=['ts_code'],dtype=str).ts_code)
   return None
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
     daily_codes=None
     if reused:
      try:
       frame=pd.read_csv(path,dtype={'trade_date':str,'ts_code':str})
       daily_codes=daily_quote_codes(frame,date)
       validate(frame,date,kind,max_null_ratio,exclude_bj,listing_dates,daily_codes)
      except (ValueError,UnicodeError,pd.errors.ParserError,pd.errors.EmptyDataError):
       rejected=target/'rejected_cache'/kind;rejected.mkdir(parents=True,exist_ok=True)
       path.replace(rejected/(date+'_'+str(time.time_ns())+'.csv'))
       reused=False
     if not reused:frame=query(pro,api,date,fields,cap,**({'limit_type':'U'} if kind=='limit_list' else {}))
     daily_codes=daily_quote_codes(frame,date)
     rows=validate(frame,date,kind,max_null_ratio,exclude_bj,listing_dates,daily_codes)
     if kind=='daily':
      missing=mapped_pre_listing_quotes(frame,listing_dates)
      if missing.any():record['known_source_gap_days'][date]={'pre_listing_unknown_pre_close':int(missing.sum())}
     if kind=='limit_list' and daily_codes is not None:
      placeholder=suspended_placeholder_mask(frame,daily_codes)
      if placeholder.any():record['known_source_gap_days'][date]={'untradable_source_placeholder_codes':frame.loc[placeholder,'ts_code'].astype(str).tolist()}
     if not reused:
      if kind=='limit_list':
       frame['limit_data_source']='limit_list_d';frame['limit_data_quality']='full';frame['strategy_compatible']=True
       if daily_codes is not None:
        placeholder=suspended_placeholder_mask(frame,daily_codes)
        frame.loc[placeholder,'limit_data_quality']='untradable_source_placeholder'
        frame.loc[placeholder,'strategy_compatible']=False
      tmp=path.with_suffix('.csv.tmp');frame.to_csv(tmp,index=False,encoding='utf-8-sig');tmp.replace(path)
     digest=hashlib.sha256(path.read_bytes()).hexdigest()
     manifest[date]={'rows':rows,'sha256':digest,'bytes':path.stat().st_size}
     if date in record['known_source_gap_days']:manifest[date]['known_source_gaps']=record['known_source_gap_days'][date]
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
