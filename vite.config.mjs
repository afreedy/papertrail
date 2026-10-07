import {defineConfig,loadEnv} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({mode})=>({
  plugins:[react(),{name:'local-account-api',configureServer(server){
    Object.assign(process.env,loadEnv(mode,process.cwd(),''));
    server.middlewares.use('/api/account',async(req,res)=>{
      try{
        let body='';for await(const chunk of req){body+=chunk;if(body.length>8192)throw new Error('Request too large');}
        req.body=body?JSON.parse(body):{};
        req.url='/api/account'+req.url;
        const {default:handler}=await import('./api/account.js');await handler(req,res);
      }catch{res.statusCode=400;res.end(JSON.stringify({error:'Invalid request.'}));}
    });
  }}],
  server:{proxy:{'/api/search':'http://127.0.0.1:8787'}},
}));
