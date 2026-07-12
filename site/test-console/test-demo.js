// MEET_RUDI personality TEST console — DEMO backend (synthetic, no network, no real engine).
// Active only when test-config.js leaves API_BASE empty. Lets you click through the whole UI
// (login: any email + password "demo") without deploying. Rudi's replies here are canned — the
// LIVE console runs the real responder engine.
(function(){
  var DEFAULT = "seed-rudi-v2";
  var PERSONAS = [{slug:"seed-rudi-v2", name:"Seed Rudi v2"}];
  var seq = 1;
  var DB = {};  // uid -> {meta, messages}

  function uid(){ return "test_demo_"+(seq++); }
  function nowISO(){ return new Date().toISOString(); }
  function preview(t){ return (t||"").slice(0,80); }
  function row(c){
    var last=c.messages[c.messages.length-1]||{};
    return { user_id:c.meta.user_id, name:c.meta.name, persona:c.meta.persona,
             persona_effective:c.meta.persona||DEFAULT, created_at:c.meta.created_at,
             last_message_at:last.at||c.meta.created_at, last_message_preview:preview(last.text),
             last_direction:last.direction||"", message_count:c.messages.length };
  }
  function md(c){
    var name=c.meta.name;
    var out=["# Conversation — "+name,"","- **Started:** "+c.meta.created_at,"- **Persona:** "+(c.meta.persona||DEFAULT),"- **Turns:** "+c.messages.length,"","---",""];
    c.messages.forEach(function(m){ out.push("**"+(m.direction==="out"?"Rudi":name)+"** _("+(m.at||"").slice(0,19).replace("T"," ")+")_: "+(m.text||"")); out.push(""); });
    return out.join("\n");
  }
  function stamp(){ var d=new Date(); function p(n){return String(n).padStart(2,"0");} return d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+"_"+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds()); }

  window.TEST_DEMO_API = {
    login:function(email,pw){ return Promise.resolve(pw==="demo"?{status:200,body:{token:"demo-token"}}:{status:401,body:{}}); },
    personalities:function(){ return Promise.resolve({status:200,body:{personalities:PERSONAS, default:DEFAULT}}); },
    list:function(){ var rows=Object.keys(DB).filter(function(k){return DB[k].meta.status!=="archived";}).map(function(k){return row(DB[k]);}); return Promise.resolve({status:200,body:{conversations:rows}}); },
    create:function(name,persona){ var id=uid(); DB[id]={meta:{user_id:id,name:name,persona:persona||"",created_at:nowISO(),status:"active"},messages:[{direction:"out",text:"👋 Hi, I'm Rudi — lovely to meet you! What's your name?",at:nowISO()}]}; return Promise.resolve({status:201,body:{conversation:row(DB[id])}}); },
    thread:function(id){ var c=DB[id]; if(!c) return Promise.resolve({status:404,body:{}}); return Promise.resolve({status:200,body:{conversation:row(c),messages:c.messages}}); },
    send:function(id,text){ var c=DB[id]; if(!c) return Promise.resolve({status:404,body:{}}); c.messages.push({direction:"in",text:text,at:nowISO()}); var reply="(demo) Thanks for sharing — tell me a bit more?"; c.messages.push({direction:"out",text:reply,at:nowISO()}); return Promise.resolve({status:200,body:{reply:reply}}); },
    del:function(id){ if(DB[id]) DB[id].meta.status="archived"; return Promise.resolve({status:200,body:{ok:true}}); },
    exportOne:function(id){ var c=DB[id]; if(!c) return Promise.resolve({status:404,body:{}}); var safe=c.meta.name.replace(/[^a-z0-9]/gi,"_").slice(0,40); return Promise.resolve({status:200,body:{filename:"Conversation_"+safe+"_"+stamp()+".md",markdown:md(c)}}); },
    exportBulk:function(scope,from,to){
      var ks=Object.keys(DB).filter(function(k){return DB[k].meta.status!=="archived";});
      if(scope==="interval") ks=ks.filter(function(k){ var d=(DB[k].meta.created_at||"").slice(0,10); return d>=from && d<=to; });
      ks.sort(function(a,b){return (DB[a].meta.created_at||"").localeCompare(DB[b].meta.created_at||"");});
      var blocks=ks.map(function(k){return md(DB[k]);});
      var body=(scope==="interval"?("# Rudi conversation export\n\n- Scope: "+from+" → "+to+"\n"):("# Rudi conversation export\n\n- Scope: all\n"));
      var PB='\n\n<div style="page-break-after: always;"></div>\n\n';
      var out=blocks.length? body+PB+blocks.join(PB) : body+"\n_(no conversations)_\n";
      return Promise.resolve({status:200,body:{filename:"Conversation_Export_"+stamp()+".md",markdown:out,count:blocks.length}});
    }
  };
})();
