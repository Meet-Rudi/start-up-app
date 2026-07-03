// MEET_RUDI operator console — DEMO backend (synthetic data, no PII, no network).
// Mirrors the meetrudi-wa-console-api response shapes so the UI (Block 3) is fully usable
// with zero integrations. Active only when config.js leaves API_BASE empty.
(function(){
  var now = Date.now();
  function iso(offsetMin){ return new Date(now - offsetMin*60000).toISOString(); }
  var WINDOW_MS = 24*3600*1000;

  // Synthetic contacts + threads (fictional; no real numbers or people).
  var DB = {
    "wa_demo0001": {
      meta:{ user_id:"wa_demo0001", display_name:"Ava (demo)", phone:"+320000000001", locale:"en",
             consent_state:"granted", status:"active", last_inbound_at:iso(8) },
      messages:[
        {id:"m1",direction:"in",type:"text",text:"Morning Rudi! I walked 6k steps yesterday 🎉",at:iso(180)},
        {id:"m2",direction:"out",type:"text",text:"That's brilliant, Ava! How did it feel?",at:iso(175),operator_id:"operator"},
        {id:"m3",direction:"in",type:"text",text:"Honestly pretty good. Legs a bit sore though.",at:iso(9)},
        {id:"m4",direction:"in",type:"text",text:"Any tips for the soreness?",at:iso(8)}
      ]
    },
    "wa_demo0002": {
      meta:{ user_id:"wa_demo0002", display_name:"Ben (demo)", phone:"+320000000002", locale:"en",
             consent_state:"granted", status:"active", last_inbound_at:iso(40) },
      messages:[
        {id:"m1",direction:"in",type:"image",text:"",at:iso(45),media:[{url:"#"}]},
        {id:"m2",direction:"in",type:"text",text:"Lunch today — is this ok for my plan?",at:iso(44)},
        {id:"m3",direction:"out",type:"text",text:"Looks balanced! Nice bit of protein there 👍",at:iso(40),operator_id:"operator"}
      ]
    },
    "wa_demo0003": {  // out of window (last inbound > 24h ago)
      meta:{ user_id:"wa_demo0003", display_name:"Carla (demo)", phone:"+320000000003", locale:"en",
             consent_state:"granted", status:"active", last_inbound_at:iso(60*30) },
      messages:[
        {id:"m1",direction:"in",type:"text",text:"Thanks for checking in last week!",at:iso(60*30)},
        {id:"m2",direction:"out",type:"text",text:"Anytime, Carla. Talk soon!",at:iso(60*29),operator_id:"operator"}
      ]
    }
  };

  function preview(m){ if(m.text) return m.text.slice(0,80); return ({image:"📷 Photo",audio:"🎤 Voice message"}[m.type]||"📎 Attachment"); }
  function inWindow(meta){ return (now - new Date(meta.last_inbound_at).getTime()) < WINDOW_MS; }
  function windowUntil(meta){ return new Date(new Date(meta.last_inbound_at).getTime()+WINDOW_MS).toISOString(); }

  function row(c){
    var last=c.messages[c.messages.length-1]||{};
    return { user_id:c.meta.user_id, display_name:c.meta.display_name, phone:c.meta.phone,
             locale:c.meta.locale, consent_state:c.meta.consent_state, status:c.meta.status,
             unread_count:c.meta._unread||0, last_message_at:last.at||"",
             last_message_preview:last.text?preview(last):preview(last), last_direction:last.direction||"",
             in_window:inWindow(c.meta), window_open_until:windowUntil(c.meta),
             keep_warm:c.meta.keep_warm!==false,
             next_proactive_at:(c.meta.keep_warm!==false&&inWindow(c.meta))?windowUntil(c.meta):"",
             next_proactive_kind:(c.meta.keep_warm!==false&&inWindow(c.meta))?"nudge":"" };
  }

  // seed unread on the ones ending with an inbound
  Object.keys(DB).forEach(function(k){ var c=DB[k]; var last=c.messages[c.messages.length-1]; c.meta._unread = last&&last.direction==="in"?(k==="wa_demo0001"?2:1):0; });

  window.DEMO_API = {
    roster:function(){
      var rows=Object.keys(DB).map(function(k){return row(DB[k]);});
      rows.sort(function(a,b){return (b.last_message_at||"").localeCompare(a.last_message_at||"");});
      return {conversations:rows};
    },
    thread:function(uid,since){
      var c=DB[uid]; if(!c) return {error:"unknown",messages:[]};
      var msgs=c.messages;
      if(since){ var i=msgs.findIndex(function(m){return m.id===since;}); msgs = i>=0? msgs.slice(i+1):[]; }
      var cursor=c.messages.length?c.messages[c.messages.length-1].id:null;
      return {user_id:uid, contact:row(c), messages:msgs.map(function(m){return m;}), cursor:cursor};
    },
    read:function(uid){ if(DB[uid]) DB[uid].meta._unread=0; return Promise.resolve({ok:true}); },
    keepwarm:function(uid,enabled){ if(DB[uid]) DB[uid].meta.keep_warm=enabled; return Promise.resolve({ok:true,keep_warm:enabled}); },
    send:function(uid,text){
      var c=DB[uid]; if(!c) return Promise.resolve({status:404,body:{error:"unknown"}});
      if(!inWindow(c.meta)) return Promise.resolve({status:409,body:{error:"out_of_window"}});
      var m={id:"o"+Date.now(),direction:"out",type:"text",text:text,at:new Date().toISOString(),operator_id:"operator",delivery_status:"queued"};
      c.messages.push(m); c.meta._unread=0;
      return Promise.resolve({status:201,body:{message:m,cursor:m.id}});
    }
  };
})();
