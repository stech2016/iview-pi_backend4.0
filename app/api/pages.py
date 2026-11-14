"""One small web page the e-mail link opens: choose a new password in any browser (phone or PC), no app needed."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>STECH iVIEW - new password</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#14171A;color:#E6E8EA;font-family:Segoe UI,Roboto,Arial,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
.c{background:#1D2125;border:1px solid #333A40;border-radius:12px;padding:26px;width:100%;max-width:400px}
h1{font-size:20px;margin:0 0 4px}p{color:#9AA3AB;font-size:14px;margin:0 0 16px}
input{width:100%;padding:12px;margin:0 0 12px;border-radius:8px;border:1px solid #333A40;background:#14171A;color:#E6E8EA;font-size:16px}
button{width:100%;padding:13px;border:0;border-radius:8px;background:#5E94C7;color:#fff;font-size:16px;font-weight:600;cursor:pointer}
button:disabled{opacity:.6}.m{margin-top:12px;font-size:14px}.e{color:#E5534B}.o{color:#3FB56B}
</style></head><body><div class="c"><h1>STECH iVIEW</h1><p>Choose a new password.</p>
<form id="f"><input id="a" type="password" placeholder="New password" autocomplete="new-password" required>
<input id="b" type="password" placeholder="Repeat the new password" autocomplete="new-password" required>
<button id="g">Update password</button></form><div id="m" class="m"></div></div>
<script>
var t=new URLSearchParams(location.search).get("token")||"",m=document.getElementById("m"),g=document.getElementById("g");
history.replaceState(null,"",location.pathname);
document.getElementById("f").onsubmit=function(e){e.preventDefault();m.className="m";m.textContent="";
var a=document.getElementById("a").value,b=document.getElementById("b").value;
if(!t){m.className="m e";m.textContent="This link is incomplete. Open it again from the email.";return}
if(a!==b){m.className="m e";m.textContent="The two passwords are not the same.";return}
g.disabled=true;
fetch("/api/v1/auth/reset-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:t,new_password:a})})
.then(function(r){return r.json().then(function(j){return[r.ok,j]})})
.then(function(x){if(x[0]){m.className="m o";m.textContent="Password updated. Open the STECH iVIEW app and sign in with the new password.";document.getElementById("f").style.display="none"}
else{var j=x[1],msg=(j&&j.error&&j.error.message)||(j&&j.detail&&JSON.stringify(j.detail))||"Could not update the password.";m.className="m e";m.textContent=msg;g.disabled=false}})
.catch(function(){m.className="m e";m.textContent="Cannot reach the server. Check the internet and try again.";g.disabled=false});};
</script></body></html>"""


@router.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
def reset_password_page():
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})
