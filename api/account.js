import {neon} from '@neondatabase/serverless';
import nodemailer from 'nodemailer';
import {createHmac,timingSafeEqual,createHash} from 'node:crypto';

const owner=process.env.ALERT_EMAIL;
const secret=process.env.SESSION_SECRET;
const sql=process.env.DATABASE_URL ? neon(process.env.DATABASE_URL) : null;
let prepared;
const send=(res,status,data)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');res.end(JSON.stringify(data));};
const signed=expiry=>{const body=String(expiry);return `${body}.${createHmac('sha256',secret).update(body).digest('hex')}`;};
export function valid(token){
  if(!secret||typeof token!=='string')return false;
  const [expiry,hash]=token.split('.');if(!/^\d+$/.test(expiry)||Number(expiry)<Date.now()||!/^[a-f0-9]{64}$/.test(hash||''))return false;
  return timingSafeEqual(Buffer.from(hash,'hex'),Buffer.from(signed(expiry).split('.')[1],'hex'));
}
const auth=req=>valid((req.headers.cookie||'').split(';').map(s=>s.trim()).find(s=>s.startsWith('ledger_session='))?.slice(15));
async function prepare(){
  if(!sql)throw new Error('Alert storage is not configured.');
  if(!prepared)prepared=(async()=>{
    await sql`CREATE SCHEMA IF NOT EXISTS public_ledger`;
    await sql`CREATE TABLE IF NOT EXISTS public_ledger.watches(source text,name text,seen jsonb NOT NULL DEFAULT '[]',last_check timestamptz,error text,checking_until timestamptz,PRIMARY KEY(source,name))`;
    await sql`CREATE TABLE IF NOT EXISTS public_ledger.events(id text PRIMARY KEY,source text,name text,record jsonb,state text NOT NULL DEFAULT 'pending',error text,created_at timestamptz DEFAULT now(),sent_at timestamptz)`;
    await sql`CREATE TABLE IF NOT EXISTS public_ledger.login_rate(id integer PRIMARY KEY,last_send timestamptz)`;
  })().catch(e=>{prepared=null;throw e;});
  return prepared;
}
const mailReady=()=>Boolean(owner&&process.env.GMAIL_APP_PASSWORD&&secret&&sql);
async function mail(subject,text){
  if(!mailReady())throw new Error('Email delivery is not configured.');
  const transport=nodemailer.createTransport({host:'smtp.gmail.com',port:465,secure:true,
    auth:{user:owner,pass:process.env.GMAIL_APP_PASSWORD},connectionTimeout:15000,socketTimeout:25000});
  return transport.sendMail({from:`Papertrail <${owner}>`,to:owner,subject,text});
}
const base=()=>process.env.PUBLIC_APP_URL||(process.env.VERCEL_PROJECT_PRODUCTION_URL?'https://'+process.env.VERCEL_PROJECT_PRODUCTION_URL:process.env.VERCEL_URL?'https://'+process.env.VERCEL_URL:'http://127.0.0.1:5187');
async function lookup(source,name,kind='trades'){
  const url=new URL('/api/search',base());url.search=new URLSearchParams({source,name,kind,limit:'100'}).toString();
  const response=await fetch(url,{signal:AbortSignal.timeout(110000)});const data=await response.json();
  if(!response.ok||data.status!=='ok')throw new Error(data.error||'Disclosure source unavailable.');
  return data;
}
export const key=(source,row)=>createHash('sha256').update(JSON.stringify([source,row.id||row.source_url])).digest('hex');
export const purchase=row=>!row.needs_review&&['purchase','p'].includes((row.transaction||'').trim().toLowerCase());
async function check(){
  await prepare();const watches=await sql`SELECT source,name FROM public_ledger.watches ORDER BY last_check NULLS FIRST LIMIT 10`;
  const result=[];
  for(const watch of watches){
    const {source,name}=watch;
    const claim=await sql`UPDATE public_ledger.watches SET checking_until=now()+interval '3 minutes' WHERE source=${source} AND name=${name} AND (checking_until IS NULL OR checking_until<now()) RETURNING seen`;
    if(!claim.length)continue;
    try{
      const data=await lookup(source,name,source==='oge'?'filings':'trades');
      const seen=new Set(claim[0].seen);const rows=data.records;
      for(const row of rows){
        const id=key(source,row);if(seen.has(id))continue;
        // Initial subscriptions are seeded; only subsequent discoveries are candidates.
        const isOge=source==='oge'&&String(row.document_type).includes('278 Transaction');
        if(!purchase(row)&&!isOge)continue;
        await sql`INSERT INTO public_ledger.events(id,source,name,record) VALUES(${id},${source},${name},${JSON.stringify(row)}::jsonb) ON CONFLICT DO NOTHING`;
      }
      // Retain all seen identities rather than forgetting old rows that leave a public window.
      const merged=[...new Set([...seen,...rows.map(r=>key(source,r))])];
      await sql`UPDATE public_ledger.watches SET seen=${JSON.stringify(merged)}::jsonb,last_check=now(),error=NULL,checking_until=NULL WHERE source=${source} AND name=${name}`;
      result.push({name,status:'checked',coverage:data.coverage});
    }catch(e){
      await sql`UPDATE public_ledger.watches SET error=${e.message},checking_until=NULL WHERE source=${source} AND name=${name}`;
      result.push({name,status:'error',error:e.message});
    }
  }
  const pending=await sql`SELECT * FROM public_ledger.events WHERE state='pending' ORDER BY created_at LIMIT 20`;
  for(const event of pending){
    const claimed=await sql`UPDATE public_ledger.events SET state='sending' WHERE id=${event.id} AND state='pending' AND EXISTS(SELECT 1 FROM public_ledger.watches WHERE source=public_ledger.events.source AND name=public_ledger.events.name) RETURNING id`;
    if(!claimed.length)continue;
    const r=event.record;const filing=event.source==='oge';
    try{
      await mail(filing?`New transaction filing: ${event.name}`:`New disclosed purchase: ${event.name}`,
        filing?`${event.name} has a newly discovered OGE transaction filing. Verify the PDF before acting; this is a filing alert, not a verified purchase.\n\n${r.document_type}\nAdded to OGE: ${r.date_added}\n${r.source_url}\n\nOpen Papertrail: ${base()}`:
        `${event.name} disclosed a purchase.\n\nAsset: ${r.asset}\nTicker: ${r.ticker||'Not reported'}\nReported amount: ${r.amount_range}\nTrade date: ${r.trade_date}\nDisclosed: ${r.filed_date}\n\nSource: ${r.source_url}\n\nThe filing reports an amount range, not the execution price.\nOpen Papertrail: ${base()}`);
      await sql`UPDATE public_ledger.events SET state='sent',sent_at=now() WHERE id=${event.id}`;
    }catch(e){
      // ponytail: SMTP has no idempotency key; uncertain failures require review, not blind resend.
      await sql`UPDATE public_ledger.events SET state='delivery_review',error=${e.message} WHERE id=${event.id}`;
    }
  }
  return result;
}

export default async function handler(req,res){
  const url=new URL(req.url,'http://localhost');const action=url.searchParams.get('action')||'status';
  let body=req.body||{};
  try{
    if(typeof body==='string')body=JSON.parse(body);
    if(action==='cron'){
      if(!process.env.CRON_SECRET||req.headers.authorization!==`Bearer ${process.env.CRON_SECRET}`)return send(res,401,{error:'Unauthorised.'});
      return send(res,200,{checks:await check()});
    }
    if(action==='status'){
      if(!auth(req))return send(res,200,{signedIn:false,configured:mailReady(),recipient:owner?owner.replace(/^(.{2}).*(@.*)$/,'$1…$2'):null});
      await prepare();
      return send(res,200,{signedIn:true,configured:mailReady(),recipient:owner,watches:await sql`SELECT source,name,last_check,error FROM public_ledger.watches ORDER BY name`,events:await sql`SELECT * FROM public_ledger.events ORDER BY created_at DESC LIMIT 50`,schedule:'Daily at 09:00 Singapore time; Check now runs an additional scan.'});
    }
    if(req.method!=='POST')return send(res,405,{error:'Use POST.'});
    const origin=req.headers.origin;
    if(origin&&origin!==new URL(base()).origin&&origin!==(process.env.VERCEL_URL?'https://'+process.env.VERCEL_URL:''))return send(res,403,{error:'Origin rejected.'});
    if(action==='login'){
      await prepare();
      if(!mailReady())return send(res,503,{error:'Email delivery is not configured.'});
      const rate=await sql`INSERT INTO public_ledger.login_rate(id,last_send) VALUES(1,now()) ON CONFLICT(id) DO UPDATE SET last_send=now() WHERE public_ledger.login_rate.last_send<now()-interval '60 seconds' RETURNING id`;
      if(!rate.length)return send(res,429,{error:'A sign-in email was just sent. Wait a minute before requesting another.'});
      const token=signed(Date.now()+30*60*1000);
      await mail('Your Papertrail sign-in link',`Sign in to your personal dashboard:\n${base()}/#session=${token}\n\nThis link expires in 30 minutes. It gives access to your watchlist and email alerts.`);
      return send(res,200,{sent:true});
    }
    if(action==='session'){
      if(!valid(body.token))return send(res,401,{error:'Sign-in link expired. Request another.'});
      const cookie=signed(Date.now()+30*24*60*60*1000);
      res.setHeader('Set-Cookie',`ledger_session=${cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000${base().startsWith('https:')?'; Secure':''}`);
      return send(res,200,{signedIn:true});
    }
    if(!auth(req))return send(res,401,{error:'Sign in to manage email alerts.'});
    await prepare();
    if(action==='logout'){res.setHeader('Set-Cookie','ledger_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0');return send(res,200,{signedIn:false});}
    if(action==='watch'||action==='unwatch'){
      const {name,source}=body;
      if(typeof name!=='string'||!name.trim()||name.length>100||!['quiver','oge'].includes(source))return send(res,400,{error:'Choose a valid politician.'});
      if(action==='unwatch'){
        await sql`DELETE FROM public_ledger.watches WHERE source=${source} AND name=${name}`;
        await sql`UPDATE public_ledger.events SET state='cancelled' WHERE source=${source} AND name=${name} AND state='pending'`;
      }
      else{
        const count=await sql`SELECT count(*)::int AS n FROM public_ledger.watches`;
        if(count[0].n>=10)return send(res,400,{error:'Follow up to 10 politicians for daily alerts.'});
        const data=await lookup(source,name,source==='oge'?'filings':'trades');
        const seen=data.records.map(r=>key(source,r));
        await sql`INSERT INTO public_ledger.watches(source,name,seen,last_check) VALUES(${source},${name},${JSON.stringify(seen)}::jsonb,now()) ON CONFLICT(source,name) DO NOTHING`;
      }
      return send(res,200,{saved:true});
    }
    if(action==='check')return send(res,200,{checks:await check()});
    if(action==='test'){await mail('Papertrail alerts are connected',`Your politician disclosure alerts are connected. Follow a politician in the dashboard to start monitoring new filings. Checks run daily at 09:00 Singapore time and whenever you press Check now.\n\n${base()}\n\nThis is a connection test; no trade was invented.`);return send(res,200,{sent:true});}
    return send(res,400,{error:'Unknown action.'});
  }catch(e){console.error('Account operation failed:',action,e.code||e.name);return send(res,e.code==='EAUTH'?503:500,{error:e.code==='EAUTH'?'Gmail rejected the email connection. A fresh Gmail app password is required before alerts can be enabled.':'The alert service could not complete this request. Try again.'});}
}
