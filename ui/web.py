from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from config import APP_NAME, VERSION, GITHUB_NAME, HOST, PORT

app = FastAPI(title=APP_NAME)


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>STRIKER // Cyberpunk Interface</title>
<style>
:root{
  --cyan:#00f6ff; --purple:#b026ff; --green:#39ff88; --red:#ff3344;
  --bg:#05070b; --panel:#080c13;
}
*{box-sizing:border-box}
body{
  margin:0;background:
  radial-gradient(circle at 50% 20%,#10152a 0,#05070b 42%,#020307 100%);
  color:var(--cyan);font-family:"Courier New",monospace;min-height:100vh;
  overflow-x:hidden;
}
body:before{
  content:"";position:fixed;inset:0;pointer-events:none;opacity:.08;
  background:repeating-linear-gradient(0deg,transparent 0 3px,#fff 4px);
}
.frame{min-height:100vh;padding:24px;border:1px solid #12363d;position:relative}
.frame:after{
  content:"";position:absolute;inset:12px;border:1px solid #182f49;
  pointer-events:none;box-shadow:0 0 30px #00f6ff22 inset;
}
.topbar{
  display:flex;justify-content:space-between;border-bottom:1px solid var(--cyan);
  padding:8px 14px;letter-spacing:2px;color:var(--green)
}
.hero{
  margin:22px auto 12px;max-width:1500px;min-height:240px;
  border:2px solid var(--cyan);position:relative;padding:25px;
  box-shadow:0 0 28px #00f6ff33, inset 0 0 40px #00f6ff0c;
}
.hero h1{
  font-size:clamp(44px,8vw,105px);margin:0;
  color:var(--cyan);letter-spacing:7px;
  text-shadow:5px 0 var(--purple),-3px 0 var(--green);
}
.hero .tag{color:var(--green);font-weight:bold}
.hero .by{color:var(--purple);font-size:22px;margin-top:10px}
.grid{
  max-width:1500px;margin:auto;display:grid;
  grid-template-columns:260px 1fr 260px;gap:16px;
}
.panel{
  background:#080c13dd;border:1px solid var(--cyan);padding:18px;
  box-shadow:0 0 20px #00f6ff15 inset;
}
.panel.purple{border-color:var(--purple)}
.panel.red{border-color:var(--red);color:var(--red)}
.title{font-weight:bold;margin-bottom:15px;color:var(--green)}
.menu{
  min-height:110px;margin:16px 0;padding:22px;border:1px solid var(--cyan);
  display:flex;align-items:center;justify-content:space-between;
  cursor:pointer;text-decoration:none;color:var(--cyan);
  transition:.15s;box-shadow:0 0 14px #00f6ff12;
}
.menu:hover{transform:translateX(4px);box-shadow:0 0 25px #00f6ff55}
.menu.purple{border-color:var(--purple);color:var(--purple)}
.menu.red{border-color:var(--red);color:var(--red)}
.num{font-size:48px;font-weight:bold}
.menu strong{font-size:20px;letter-spacing:1px}
pre{white-space:pre-wrap;line-height:1.45}
.footer{text-align:center;color:var(--cyan);padding:25px;letter-spacing:3px}
.hidden{display:none}
@media(max-width:1000px){.grid{grid-template-columns:1fr}.hero{min-height:auto}.panel{min-height:auto}}
</style>
</head>
<body>
<div class="frame">
  <div class="topbar">
    <span>SYSTEM // ONLINE // ACCESS // v{{VERSION}}</span>
    <span>LOCAL // JUST CODE</span>
  </div>

  <section class="hero">
    <div class="tag">/// CYBERPUNK CONTROL INTERFACE ///</div>
    <h1>STRIKER</h1>
    <div class="by">BY GitHub // {{GITHUB}}</div>
  </section>

  <div class="grid">
    <aside>
      <div class="panel">
        <div class="title">[ SYSTEM ]</div>
        <pre>> INITIALIZING...
> MODULES LOADED
> NETWORK READY
> STATUS: ONLINE</pre>
      </div>
      <div class="panel">
        <div class="title">[ LOCAL STATUS ]</div>
        <pre>HOST   : 127.0.0.1
PORT   : {{PORT}}
STATUS : ONLINE</pre>
      </div>
    </aside>

    <main class="panel">
      <div class="title">[ STRIKER CONTROL PANEL ]</div>
      <a class="menu" href="/striker"><span class="num">1</span><strong>OPEN STRIKER PAGE</strong><span>&gt;&gt;&gt;</span></a>
      <a class="menu purple" href="/settings"><span class="num">2</span><strong>SETTINGS</strong><span>&gt;&gt;&gt;</span></a>
      <a class="menu red" href="javascript:window.close()"><span class="num">3</span><strong>QUIT</strong><span>&gt;&gt;&gt;</span></a>
      <div style="text-align:center;color:var(--green);padding:10px">[ SELECT AN OPTION ]</div>
    </main>

    <aside>
      <div class="panel purple">
        <div class="title" style="color:var(--purple)">[ STRIKER ]</div>
        <pre>> CONTROL PANEL
> MULTI-THREAD UI
> CUSTOM CONFIG
> LOCAL MODE</pre>
      </div>
      <div class="panel red">
        <div class="title" style="color:var(--red)">[ WARNING ]</div>
        <pre>UI / CONTROL LAYER ONLY

No attack implementation
is included in this project.</pre>
      </div>
    </aside>
  </div>

  <div class="footer">GITHUB // BY {{GITHUB}} // JUST CODE</div>
</div>
</body>
</html>'''


def render(page: str = "home") -> str:
    html = HTML.replace("{{VERSION}}", VERSION).replace("{{GITHUB}}", GITHUB_NAME).replace("{{PORT}}", str(PORT))

    if page == "striker":
        content = '''<section class="hero"><div class="tag">/// STRIKER PAGE ///</div><h1>STRIKER</h1><div class="by">SYSTEM READY // UI PLACEHOLDER</div></section>
        <div class="grid"><main class="panel"><div class="title">[ STRIKER STATUS ]</div><pre>SYSTEM      : READY
INTERFACE   : LOCAL HTTP
NETWORK     : NOT CONFIGURED
ENGINE      : UI PLACEHOLDER

<a style="color:var(--cyan)" href="/">[ BACK ]</a></pre></main></div>'''
        return html.replace('<div class="frame">', '<div class="frame">', 1).replace(
            '<div class="topbar">', '<div class="topbar"><span>STRIKER // CONTROL</span><span>LOCAL</span></div><div class="hidden">', 1
        ).replace('</div>
</body>', '</div>
</body>', 1).replace(
            '<section class="hero">', content + '<!--', 1
        ).replace('</div>
</body>', '--></div>
</body>', 1)

    if page == "settings":
        return html.replace('<section class="hero">', '''<section class="hero"><div class="tag">/// SETTINGS ///</div><h1>SETTINGS</h1><div class="by">CYBERPUNK THEME</div></section>
        <div class="grid"><main class="panel"><div class="title">[ CONFIGURATION ]</div><pre>HOST        : 127.0.0.1
WEB PORT    : 8080
THEME       : CYBERPUNK
ACCENT      : CYAN / PURPLE / GREEN
ERROR       : RED

These values currently control the UI only.

<a style="color:var(--cyan)" href="/">[ BACK ]</a></pre></main></div><!--''', 1).replace('</div>
</body>', '--></div>
</body>', 1)

    return html


@app.get("/", response_class=HTMLResponse)
async def home():
    return render("home")


@app.get("/striker", response_class=HTMLResponse)
async def striker():
    return render("striker")


@app.get("/settings", response_class=HTMLResponse)
async def settings():
    return render("settings")


def run_web():
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run_web()
