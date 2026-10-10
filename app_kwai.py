#!/usr/bin/env python3
"""Painel local para o kwai_dl. Uso: python app_kwai.py -> http://localhost:8001"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import kwai_dl

PORTA = 8001
AQUI = Path(__file__).parent
ARQ_LOG = AQUI / "logs" / "kwai_dl.log"
lock = threading.Lock()
parar_evt = threading.Event()
confirma_evt = threading.Event()
decisao = {"sim": False}
estado = {}
ATIVOS = ("analisando", "aguardando", "baixando")


def cmd_py(args):
    """Comando para rodar este mesmo Python. No PowerShell, caminho entre aspas exige '&'."""
    return f'{"& " if os.name == "nt" else ""}"{sys.executable}" {args}'


def zerar():
    estado.update(status="ocioso", logs=[], itens={}, total=0, ok=0, falhas=0,
                  puladas=0, pasta="", encontrados=0, resumo=None)


zerar()


def log(msg):
    linha = f"{time.strftime('%H:%M:%S')}  {msg}"
    kwai_dl.console.print(linha)
    try:
        ARQ_LOG.parent.mkdir(exist_ok=True)
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d')} {linha}\n")
    except OSError:
        pass
    with lock:
        estado["logs"].append(linha)


def evento(tipo, **d):
    with lock:
        if tipo == "encontrados":
            estado["encontrados"] = d["n"]
        elif tipo == "progresso":
            it = estado["itens"].setdefault(d["id"], {"id": d["id"]})
            it.update({k: v for k, v in d.items() if k != "id"})
        elif tipo == "total":
            estado["total"] = d["total"]
        elif tipo == "item":
            it = estado["itens"].setdefault(d["id"], {"id": d["id"]})
            it.update({k: v for k, v in d.items() if k != "id"})
            chave = {"ok": "ok", "falhou": "falhas", "pulado": "puladas"}.get(d["status"])
            if chave:
                estado[chave] += 1


def confirmar(info):
    """Chamado pelo kwai_dl depois da análise; espera a resposta do modal."""
    with lock:
        estado["resumo"] = info
        estado["status"] = "aguardando"
    decisao["sim"] = False
    confirma_evt.clear()
    for _ in range(3600):  # até 30 min
        if confirma_evt.wait(0.5) or parar_evt.is_set():
            break
    with lock:
        estado["resumo"] = None
        estado["status"] = "baixando"
    return decisao["sim"] and not parar_evt.is_set()


def responder(sim):
    if estado["status"] != "aguardando":
        return {"erro": "Nada aguardando confirmação."}
    decisao["sim"] = bool(sim)
    if not sim:
        parar_evt.set()
    confirma_evt.set()
    return {"ok": True}


def trabalho(c):
    try:
        kwai_dl.executar(c["url"], c["pasta"], c["audio"], c["formato"], c["max_scrolls"],
                         c["pausa_ms"], c["headful"], c["limite"], log, evento, parar_evt,
                         confirmar, paralelo=c["paralelo"])
        final = "parado" if parar_evt.is_set() else "concluido"
    except ModuleNotFoundError as e:
        log(f"ERRO: falta o módulo '{e.name}' neste Python ({sys.executable}).")
        log(f"Instale com: {cmd_py('-m pip install -r requirements.txt')}")
        log(f"e depois: {cmd_py('-m playwright install chromium')}")
        final = "erro"
    except Exception as e:
        log(f"ERRO: {e}")
        final = "erro"
    with lock:
        estado["status"] = final


def iniciar(d):
    with lock:
        if estado["status"] in ATIVOS:
            return {"erro": "Já existe um download em andamento."}
    url = (d.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"erro": "Cole a URL completa do perfil (https://...)."}
    pasta = (d.get("pasta") or "").strip()
    if not pasta or not Path(pasta).is_dir():
        return {"erro": "Escolha a pasta de destino."}
    c = dict(
        url=url, pasta=pasta,
        audio=d.get("modo") == "audio",
        formato=d.get("formato") if d.get("formato") in ("mp3", "m4a", "wav", "opus") else "mp3",
        max_scrolls=max(1, int(d.get("max_scrolls") or 200)),
        pausa_ms=max(300, int(d.get("pausa_ms") or 1500)),
        limite=max(0, int(d.get("limite") or 0)), headful=bool(d.get("headful")),
        paralelo=min(8, max(1, int(d.get("paralelo") or 3))),
    )
    parar_evt.clear()
    confirma_evt.clear()
    with lock:
        zerar()
        estado["status"] = "analisando"
        estado["pasta"] = str(Path(c["pasta"]).resolve())
    threading.Thread(target=trabalho, args=(c,), daemon=True).start()
    return {"ok": True}


SELETOR = (
    "import sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); r.update()\n"
    "d = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "print(filedialog.askdirectory(parent=r, title='Escolha a pasta de destino',"
    " initialdir=d or None))\n"
)


def escolher_pasta(inicial=""):
    """Abre o seletor de pastas nativo do sistema (num subprocesso, por causa do tkinter)."""
    ini = inicial if inicial and Path(inicial).is_dir() else ""
    try:
        r = subprocess.run([sys.executable, "-c", SELETOR, ini], capture_output=True,
                           text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"erro": "O seletor de pastas demorou demais e foi fechado."}
    if r.returncode != 0:
        log(f"Seletor de pastas falhou: {(r.stderr or '').strip()[-200:]}")
        return {"erro": "Não consegui abrir o seletor de pastas (tkinter indisponível?)."}
    p = r.stdout.strip()
    return {"pasta": str(Path(p))} if p else {"cancelado": True}


def abrir_pasta():
    p = estado["pasta"]
    if not p or not Path(p).exists():
        return {"erro": "A pasta ainda não existe."}
    if hasattr(os, "startfile"):
        os.startfile(p)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
    return {"ok": True}


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):  # bloqueia DNS rebinding
        return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._json({"erro": "host inválido"}, 403)
        if u.path == "/":
            b = (AQUI / "painel_kwai.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/estado":
            desde = int(parse_qs(u.query).get("desde", ["0"])[0])
            with lock:
                r = {k: v for k, v in estado.items() if k not in ("logs", "itens")}
                r["logs"] = estado["logs"][desde:]
                r["n_logs"] = len(estado["logs"])
                r["itens"] = list(estado["itens"].values())
            self._json(r)
        else:
            self._json({"erro": "não encontrado"}, 404)

    def do_POST(self):
        if not self._host_ok() or "application/json" not in self.headers.get("Content-Type", ""):
            return self._json({"erro": "requisição inválida"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/iniciar":
                return self._json(iniciar(d))
            if self.path == "/api/escolher-pasta":
                return self._json(escolher_pasta(d.get("inicial") or ""))
            if self.path == "/api/confirmar":
                return self._json(responder(d.get("sim")))
            if self.path == "/api/parar":
                parar_evt.set()
                log("Pedido de parada recebido...")
                return self._json({"ok": True})
            if self.path == "/api/abrir-pasta":
                return self._json(abrir_pasta())
        except Exception as e:
            return self._json({"erro": str(e)}, 400)
        self._json({"erro": "não encontrado"}, 404)

    def log_message(self, *a):
        pass


def checar_dependencias():
    import importlib.util
    if not hasattr(kwai_dl, "executar"):
        sys.exit(f"\nERRO: o kwai_dl.py carregado está desatualizado (sem 'executar'):\n"
                 f"  {Path(kwai_dl.__file__).resolve()}\n"
                 "Substitua-o pela versão mais recente e rode de novo.")
    falta = [m for m in ("playwright",) if importlib.util.find_spec(m) is None]
    if falta:
        print(f"\nATENÇÃO: faltam módulos neste Python: {', '.join(falta)}")
        print(f"Instale com (PowerShell; no cmd.exe tire o '&' do começo):")
        print(f"  {cmd_py('-m pip install -r requirements.txt')}")
        print(f"  {cmd_py('-m playwright install chromium')}\n")


if __name__ == "__main__":
    checar_dependencias()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), H)
    print(f"Painel em http://localhost:{PORTA}  (Ctrl+C para sair)")
    threading.Timer(1, lambda: webbrowser.open(f"http://localhost:{PORTA}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        parar_evt.set()
